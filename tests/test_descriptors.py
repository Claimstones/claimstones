"""Check-input parsing and derivation (bitcoin/descriptors.py,
bitcoin/ripemd160.py): RIPEMD-160 vectors, BIP-32 public derivation
(test vector 1), BIP-84 and BIP-86 address vectors, Bitcoin Core's
descriptor checksum, classification and refusals, the gap-limit walk."""

from __future__ import annotations

import unittest

from claimstones.bitcoin.descriptors import (
    GAP_LIMIT, CheckInputError, parse_inputs, parse_line, parse_xpub, scan_branch,
)
from claimstones.bitcoin.ripemd160 import _ripemd160_pure, hash160, ripemd160

# BIP-32 test vector 1: M/0' and its non-hardened child M/0'/1.
V1_M0H = ("xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZe"
          "NK1VTsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw")
V1_M0H_1 = ("xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbW"
            "MiGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ")

# BIP-84 test vector: account 0 zpub of the "abandon ... about" seed.
BIP84_ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1AD"
              "qtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs")
BIP84_FIRST = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
BIP84_SECOND = "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"
BIP84_CHANGE = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"

# BIP-86 test vector: account 0 xpub (m/86'/0'/0') of the same seed.
BIP86_XPUB = ("xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVk"
              "y7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ")
BIP86_FIRST = "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"
BIP86_SECOND = "bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh"
BIP86_CHANGE = "bc1p3qkhfews2uk44qtvauqyr2ttdsw7svhkl9nkm9s9c3x4ax5h60wqwruhk7"

# Bitcoin Core doc/descriptors.md checksum example.
CORE_DESC = ("wpkh([d34db33f/84h/0h/0h]xpub6DJ2dNUysrn5Vt36jH2KLBT2i1auw1tTSSomg8"
             "PhqNiUtx8QX2SvC9nrHu81fT41fvDUnhMjEzQgXnQjKEu3oaqMSzhSrHMxyyoEAmUHQ"
             "bY/0/*)#cjjspncu")


class Ripemd160Tests(unittest.TestCase):
    def test_published_vectors(self):
        for data, want in [
            (b"", "9c1185a5c5e9fc54612808977ee8f548b2258d31"),
            (b"abc", "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"),
            (b"message digest", "5d0689ef49d2fae572b881b123a85ffa21595f36"),
            (b"a" * 1000000, "52783243c1697bdbe16d37f97f68f08325dc1528"),
        ]:
            self.assertEqual(ripemd160(data).hex(), want)
            self.assertEqual(_ripemd160_pure(data).hex(), want)

    def test_pure_matches_on_block_boundaries(self):
        for n in (0, 1, 55, 56, 63, 64, 65, 119, 120, 128, 300):
            data = bytes(range(256))[:n] if n <= 256 else b"x" * n
            self.assertEqual(_ripemd160_pure(data), ripemd160(data))

    def test_hash160(self):
        # HASH160 of the generator point's compressed pubkey (well known).
        g = bytes.fromhex(
            "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
        self.assertEqual(hash160(g).hex(),
                         "751e76e8199196d454941c45d1b3a323f1433bd6")


class Bip32Tests(unittest.TestCase):
    def test_vector1_public_child(self):
        xpub, pinned = parse_xpub(V1_M0H)
        self.assertIsNone(pinned)
        self.assertEqual(xpub.child(1).serialize(), V1_M0H_1)

    def test_hardened_child_refused(self):
        xpub, _ = parse_xpub(V1_M0H)
        with self.assertRaises(CheckInputError):
            xpub.child(0x80000000)

    def test_slip132_normalizes_and_pins(self):
        xpub, pinned = parse_xpub(BIP84_ZPUB)
        self.assertEqual(pinned, "wpkh")
        self.assertEqual(xpub.network, "main")
        self.assertTrue(xpub.serialize().startswith("xpub"))

    def test_private_and_multisig_keys_refused(self):
        xprv = ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChk"
                "VvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi")
        with self.assertRaises(CheckInputError) as ctx:
            parse_xpub(xprv)
        self.assertIn("PRIVATE", str(ctx.exception))
        with self.assertRaises(CheckInputError):
            parse_line(xprv)


class DerivationVectorTests(unittest.TestCase):
    def test_bip84_zpub_line(self):
        branches = parse_line(BIP84_ZPUB)
        # SLIP-132 pins wpkh: receive and change only.
        self.assertEqual(len(branches), 2)
        receive, change = branches
        self.assertTrue(all(b.ranged for b in branches))
        self.assertEqual(receive.derive(0).address, BIP84_FIRST)
        self.assertEqual(receive.derive(1).address, BIP84_SECOND)
        self.assertEqual(change.derive(0).address, BIP84_CHANGE)

    def test_bip86_tr_descriptor(self):
        branches = parse_line(f"tr({BIP86_XPUB}/0/*)")
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0].derive(0).address, BIP86_FIRST)
        self.assertEqual(branches[0].derive(1).address, BIP86_SECOND)
        change = parse_line(f"tr({BIP86_XPUB}/1/*)")[0]
        self.assertEqual(change.derive(0).address, BIP86_CHANGE)

    def test_multipath_group(self):
        branches = parse_line(f"tr({BIP86_XPUB}/<0;1>/*)")
        self.assertEqual(len(branches), 2)
        self.assertEqual(branches[0].derive(0).address, BIP86_FIRST)
        self.assertEqual(branches[1].derive(0).address, BIP86_CHANGE)

    def test_plain_xpub_scans_all_four_types(self):
        branches = parse_line(BIP86_XPUB)
        self.assertEqual(len(branches), 8)  # 4 types x receive/change
        kinds = {b.descriptor.split("(")[0] for b in branches}
        self.assertEqual(kinds, {"pkh", "sh", "wpkh", "tr"})
        tr_receive = [b for b in branches
                      if b.descriptor.startswith("tr(") and b._path == (0,)]
        self.assertEqual(tr_receive[0].derive(0).address, BIP86_FIRST)

    def test_origin_carried_to_derived_key(self):
        desc = f"tr([d34db33f/86h/0h/0h]{BIP86_XPUB}/0/*)"
        derived = parse_line(desc)[0].derive(0)
        self.assertEqual(derived.origin.fingerprint.hex(), "d34db33f")
        h = 0x80000000
        self.assertEqual(derived.origin.path, (86 + h, 0 + h, 0 + h, 0, 0))
        bare = parse_line(f"tr({BIP86_XPUB}/0/*)")[0].derive(0)
        self.assertIsNone(bare.origin)

    def test_script_types_from_one_key(self):
        xpub, _ = parse_xpub(BIP86_XPUB)
        pub = xpub.derive((0, 0)).pubkey
        h = hash160(pub).hex()
        by_kind = {}
        for line in (f"pkh({BIP86_XPUB}/0/*)", f"wpkh({BIP86_XPUB}/0/*)",
                     f"sh(wpkh({BIP86_XPUB}/0/*))"):
            branch = parse_line(line)[0]
            by_kind[line.split("(")[0]] = branch.derive(0).script.hex()
        self.assertEqual(by_kind["pkh"], f"76a914{h}88ac")
        self.assertEqual(by_kind["wpkh"], f"0014{h}")
        self.assertEqual(by_kind["sh"],
                         "a914" + hash160(bytes.fromhex(f"0014{h}")).hex() + "87")


class DescriptorParseTests(unittest.TestCase):
    def test_core_checksum_accepted(self):
        branches = parse_line(CORE_DESC)
        self.assertEqual(len(branches), 1)
        self.assertTrue(branches[0].ranged)

    def test_bad_checksum_refused(self):
        broken = CORE_DESC[:-1] + ("p" if CORE_DESC[-1] != "p" else "q")
        with self.assertRaises(CheckInputError) as ctx:
            parse_line(broken)
        self.assertIn("checksum", str(ctx.exception))

    def test_hardened_after_xpub_refused(self):
        with self.assertRaises(CheckInputError) as ctx:
            parse_line(f"tr({BIP86_XPUB}/0h/*)")
        self.assertIn("account-level", str(ctx.exception))

    def test_multisig_descriptor_refused(self):
        with self.assertRaises(CheckInputError):
            parse_line(f"sh(sortedmulti(2,{BIP86_XPUB}))")

    def test_bare_xonly_key_in_tr(self):
        x = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        branch = parse_line(f"tr({x})")[0]
        self.assertFalse(branch.ranged)
        self.assertEqual(branch.derive(0).script[:2], bytes([0x51, 0x20]))
        with self.assertRaises(CheckInputError):
            parse_line(f"wpkh({x})")  # x-only outside tr()

    def test_addresses(self):
        line = BIP84_FIRST
        branch = parse_line(line)[0]
        self.assertFalse(branch.ranged)
        self.assertEqual(branch.derive(0).address, line)
        # The pasted address decodes to the same script the zpub derives.
        receive = parse_line(BIP84_ZPUB)[0]
        self.assertEqual(branch.derive(0).script, receive.derive(0).script)
        legacy = parse_line("1BitcoinEaterAddressDontSendf59kuE")[0]
        self.assertEqual(legacy.derive(0).script[:3], bytes([0x76, 0xA9, 0x14]))
        with self.assertRaises(CheckInputError):
            parse_line("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx")  # wrong net

    def test_network_pinning(self):
        with self.assertRaises(CheckInputError) as ctx:
            parse_line(BIP84_ZPUB, network="regtest")
        self.assertIn("mainnet", str(ctx.exception))

    def test_parse_inputs_dedupes(self):
        branches = parse_inputs([BIP84_FIRST, BIP84_FIRST, BIP84_ZPUB])
        self.assertEqual(len(branches), 3)  # address + receive + change
        with self.assertRaises(CheckInputError):
            parse_inputs(["", "   "])


class GapLimitTests(unittest.TestCase):
    def test_no_hits_scans_one_window(self):
        branch = parse_line(f"tr({BIP86_XPUB}/0/*)")[0]
        probed = []
        n = scan_branch(branch, lambda i, d: probed.append(i) or False)
        self.assertEqual(n, GAP_LIMIT)
        self.assertEqual(probed, list(range(GAP_LIMIT)))

    def test_hit_extends_the_window(self):
        branch = parse_line(f"tr({BIP86_XPUB}/0/*)")[0]
        n = scan_branch(branch, lambda i, d: i in (0, 15))
        self.assertEqual(n, 15 + GAP_LIMIT + 1)
        # A hit a full gap beyond the last one is never reached: that is
        # what the gap limit means.
        n = scan_branch(branch, lambda i, d: i in (0, 25))
        self.assertEqual(n, GAP_LIMIT + 1)

    def test_cap_holds(self):
        branch = parse_line(f"tr({BIP86_XPUB}/0/*)")[0]
        n = scan_branch(branch, lambda i, d: True, max_range=50)
        self.assertEqual(n, 50)

    def test_fixed_branch_probed_once(self):
        branch = parse_line(BIP84_FIRST)[0]
        probed = []
        n = scan_branch(branch, lambda i, d: probed.append((i, d.address)) or False)
        self.assertEqual(n, 1)
        self.assertEqual(probed, [(0, BIP84_FIRST)])


if __name__ == "__main__":
    unittest.main()
