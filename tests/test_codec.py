"""Tests for the release 1 envelope codec (docs/spec/envelope.md).

The generic parsing cases are ported from ord's runestone.rs test module;
the LAND, LIFT and single-pointer cases are Claimstones v1.

Run:  python -m unittest tests.test_codec   (from the repo root)
"""

from __future__ import annotations

import unittest

from claimstones.protocol.codec import (
    MAGIC,
    OP_PUSHNUM_15,
    OP_RETURN,
    ORCHID,
    U128_MAX,
    U32_MAX,
    U64_MAX,
    AssetId,
    Cenotaph,
    Edict,
    Envelope,
    Flaw,
    Tag,
    decipher,
    encipher,
    land_script,
    send_script,
    varint_decode,
    varint_encode,
)

OP_PUSHNUM_13 = 0x5D  # runestone magic; must never match


class FakeOut:
    def __init__(self, script_pubkey: bytes):
        self.script_pubkey = script_pubkey


class FakeTx:
    def __init__(self, outputs):
        self.outputs = outputs


def push(data: bytes) -> bytes:
    assert len(data) <= 75
    return bytes([len(data)]) + data


def payload_of(integers) -> bytes:
    out = bytearray()
    for n in integers:
        out.extend(varint_encode(n))
    return bytes(out)


def tx_from_integers(integers, n_extra_outputs: int = 0) -> FakeTx:
    script = MAGIC + push(payload_of(integers))
    return FakeTx([FakeOut(script)] + [FakeOut(b"") for _ in range(n_extra_outputs)])


def decipher_ints(integers, n_extra_outputs: int = 0) -> object:
    return decipher(tx_from_integers(integers, n_extra_outputs))


def orchid_edict(amount: int, output: int) -> Edict:
    return Edict(ORCHID, amount, output)


class VarintTests(unittest.TestCase):
    def test_round_trips(self):
        for n in [0, 1, 127, 128, 255, 256, U128_MAX] + [1 << i for i in range(128)]:
            encoded = varint_encode(n)
            decoded, length = varint_decode(encoded)
            self.assertEqual(decoded, n)
            self.assertEqual(length, len(encoded))

    def test_uint128_max_encoding(self):
        self.assertEqual(len(varint_encode(U128_MAX)), 19)

    def test_unterminated(self):
        with self.assertRaises(Exception):
            varint_decode(bytes([128]))

    def test_overlong(self):
        with self.assertRaises(Exception):
            varint_decode(bytes([128] * 19 + [0]))

    def test_overflow(self):
        with self.assertRaises(Exception):
            varint_decode(bytes([128] * 18 + [64]))


class LocationTests(unittest.TestCase):
    def test_no_outputs_returns_none(self):
        self.assertIsNone(decipher(FakeTx([])))

    def test_non_op_return_output_returns_none(self):
        self.assertIsNone(decipher(FakeTx([FakeOut(push(b""))])))

    def test_bare_op_return_returns_none(self):
        self.assertIsNone(decipher(FakeTx([FakeOut(bytes([OP_RETURN]))])))

    def test_non_matching_op_return_returns_none(self):
        self.assertIsNone(
            decipher(FakeTx([FakeOut(bytes([OP_RETURN]) + push(b"FOOO"))]))
        )

    def test_runestone_magic_is_not_an_envelope(self):
        script = bytes([OP_RETURN, OP_PUSHNUM_13]) + push(payload_of([Tag.LAND, 0]))
        self.assertIsNone(decipher(FakeTx([FakeOut(script)])))

    def test_runestone_and_envelope_in_same_tx_are_independent(self):
        runestone = bytes([OP_RETURN, OP_PUSHNUM_13]) + push(payload_of([20, 1, 20, 0]))
        envelope = MAGIC + push(payload_of([Tag.LAND, 0]))
        tx = FakeTx([FakeOut(runestone), FakeOut(envelope), FakeOut(b"")])
        self.assertEqual(decipher(tx), Envelope(land=True))

    def test_malformed_first_opcode_returns_none(self):
        # a truncated push: skipped when searching for the envelope
        self.assertIsNone(decipher(FakeTx([FakeOut(bytes([0x04]))])))

    def test_envelope_in_second_output(self):
        script = MAGIC + push(payload_of([Tag.BODY, 0, 0, 2, 0]))
        self.assertEqual(
            decipher(FakeTx([FakeOut(b""), FakeOut(script)])),
            Envelope(edicts=[orchid_edict(2, 0)]),
        )

    def test_envelope_after_non_matching_op_return(self):
        first = bytes([OP_RETURN]) + push(b"FOO")
        second = MAGIC + push(payload_of([Tag.BODY, 0, 0, 2, 0]))
        self.assertEqual(
            decipher(FakeTx([FakeOut(first), FakeOut(second)])),
            Envelope(edicts=[orchid_edict(2, 0)]),
        )

    def test_first_matching_output_wins(self):
        first = MAGIC + push(payload_of([Tag.LAND, 0]))
        second = MAGIC + push(payload_of([Tag.LIFT, 0]))
        self.assertEqual(
            decipher(FakeTx([FakeOut(first), FakeOut(second)])),
            Envelope(land=True),
        )

    def test_payload_pushes_are_concatenated(self):
        parts = [
            varint_encode(Tag.LAND),
            varint_encode(0),
            varint_encode(Tag.BODY),
            varint_encode(0),
            varint_encode(0),
            varint_encode(2),
            varint_encode(0),
        ]
        script = MAGIC + b"".join(push(p) for p in parts)
        self.assertEqual(
            decipher(FakeTx([FakeOut(script)])),
            Envelope(edicts=[orchid_edict(2, 0)], land=True),
        )


class ScriptFlawTests(unittest.TestCase):
    def test_invalid_script_postfix_is_cenotaph(self):
        script = MAGIC + bytes([0x04])  # truncated push
        self.assertEqual(
            decipher(FakeTx([FakeOut(script)])), Cenotaph(flaw=Flaw.INVALID_SCRIPT)
        )

    def test_non_pushdata_opcode_is_cenotaph(self):
        script = MAGIC + bytes([0x69])  # OP_VERIFY
        self.assertEqual(decipher(FakeTx([FakeOut(script)])), Cenotaph(flaw=Flaw.OPCODE))

    def test_pushnum_opcode_is_cenotaph(self):
        script = MAGIC + bytes([0x51])  # OP_PUSHNUM_1
        self.assertEqual(decipher(FakeTx([FakeOut(script)])), Cenotaph(flaw=Flaw.OPCODE))

    def test_empty_envelope(self):
        self.assertEqual(decipher(FakeTx([FakeOut(MAGIC)])), Envelope())

    def test_invalid_varint_is_cenotaph(self):
        script = MAGIC + push(bytes([128]))
        self.assertEqual(decipher(FakeTx([FakeOut(script)])), Cenotaph(flaw=Flaw.VARINT))

    def test_all_pushdata_opcodes_are_valid(self):
        for i in range(79):
            script = bytearray(MAGIC) + bytes([i])
            if i <= 75:
                for j in range(i):
                    script.append(1 if j % 2 == 0 else 0)
                if i % 2 == 1:
                    script.extend([1, 1])
            elif i == 76:
                script.append(0)
            elif i == 77:
                script.extend([0, 0])
            elif i == 78:
                script.extend([0, 0, 0, 0])
            self.assertEqual(
                decipher(FakeTx([FakeOut(bytes(script))])), Envelope(), msg=f"opcode {i}"
            )

    def test_all_non_pushdata_opcodes_are_invalid(self):
        for i in range(79, 256):
            script = MAGIC + bytes([i])
            self.assertEqual(
                decipher(FakeTx([FakeOut(script)])),
                Cenotaph(flaw=Flaw.OPCODE),
                msg=f"opcode {i}",
            )


class TagTests(unittest.TestCase):
    def test_tag_numbers(self):
        self.assertEqual((Tag.BODY, Tag.POINTER, Tag.LIFT, Tag.LAND, Tag.NOP), (0, 22, 26, 28, 127))
        self.assertEqual(OP_PUSHNUM_15, 0x5F)
        self.assertEqual(MAGIC, bytes([0x6A, 0x5F]))

    def test_land(self):
        self.assertEqual(decipher_ints([Tag.LAND, 0]), Envelope(land=True))

    def test_land_with_nonzero_value_is_cenotaph_without_land(self):
        self.assertEqual(
            decipher_ints([Tag.LAND, 1]),
            Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG, land=False),
        )

    def test_duplicate_land_is_cenotaph_with_land(self):
        self.assertEqual(
            decipher_ints([Tag.LAND, 0, Tag.LAND, 0]),
            Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG, land=True),
        )

    def test_lift_is_cenotaph(self):
        self.assertEqual(decipher_ints([Tag.LIFT, 0]), Cenotaph(flaw=Flaw.LIFT))
        self.assertEqual(decipher_ints([Tag.LIFT, 5, Tag.BODY, 0, 0, 2, 0]), Cenotaph(flaw=Flaw.LIFT))

    def test_lift_with_land_is_cenotaph_with_land(self):
        self.assertEqual(
            decipher_ints([Tag.LAND, 0, Tag.LIFT, 0]),
            Cenotaph(flaw=Flaw.LIFT, land=True),
        )

    def test_nop_is_ignored(self):
        self.assertEqual(
            decipher_ints([Tag.NOP, 100, Tag.NOP, 200, Tag.BODY, 0, 0, 2, 0]),
            Envelope(edicts=[orchid_edict(2, 0)]),
        )

    def test_unrecognized_odd_tag_is_ignored(self):
        self.assertEqual(decipher_ints([1, 0, 3, 0, 125, 0]), Envelope())

    def test_unrecognized_even_tag_is_cenotaph(self):
        for tag in [2, 4, 6, 8, 20, 24, 126]:  # incl. old RULE, PARAMS, CLAIM
            self.assertEqual(
                decipher_ints([tag, 0, Tag.BODY, 0, 0, 2, 0]),
                Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG),
                msg=str(tag),
            )

    def test_tag_with_no_value_is_cenotaph(self):
        self.assertEqual(decipher_ints([Tag.LAND, 0, Tag.LAND]), Cenotaph(flaw=Flaw.TRUNCATED_FIELD, land=True))
        self.assertEqual(decipher_ints([Tag.NOP]), Cenotaph(flaw=Flaw.TRUNCATED_FIELD))

    def test_tag_values_are_not_parsed_as_tags(self):
        self.assertEqual(
            decipher_ints([Tag.NOP, Tag.LIFT, Tag.BODY, 0, 0, 2, 0]),
            Envelope(edicts=[orchid_edict(2, 0)]),
        )


class PointerTests(unittest.TestCase):
    def test_pointer(self):
        self.assertEqual(decipher_ints([Tag.POINTER, 1], n_extra_outputs=1), Envelope(pointer=1))

    def test_pointer_out_of_range_is_cenotaph(self):
        self.assertEqual(decipher_ints([Tag.POINTER, 1]), Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG))
        self.assertEqual(decipher_ints([Tag.POINTER, U128_MAX]), Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG))

    def test_pointer_to_op_return_is_cenotaph(self):
        self.assertEqual(decipher_ints([Tag.POINTER, 0]), Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG))
        tx = tx_from_integers([Tag.POINTER, 1])
        tx.outputs.append(FakeOut(bytes([OP_RETURN]) + push(b"x")))
        self.assertEqual(decipher(tx), Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG))

    def test_two_pointers_is_cenotaph(self):
        self.assertEqual(
            decipher_ints([Tag.POINTER, 1, Tag.POINTER, 2], n_extra_outputs=2),
            Cenotaph(flaw=Flaw.UNRECOGNIZED_EVEN_TAG),
        )


class EdictTests(unittest.TestCase):
    def test_single_edict(self):
        self.assertEqual(decipher_ints([Tag.BODY, 0, 0, 2, 0]), Envelope(edicts=[orchid_edict(2, 0)]))

    def test_multiple_edicts_delta_coded(self):
        self.assertEqual(
            decipher_ints([Tag.BODY, 0, 0, 2, 0, 1, 1, 5, 0, 0, 3, 7, 0]),
            Envelope(edicts=[orchid_edict(2, 0), Edict(AssetId(1, 1), 5, 0), Edict(AssetId(1, 4), 7, 0)]),
        )

    def test_edict_with_zero_block_nonzero_tx_is_cenotaph(self):
        self.assertEqual(decipher_ints([Tag.BODY, 0, 1, 2, 0]), Cenotaph(flaw=Flaw.EDICT_ASSET_ID))

    def test_overflowing_edict_delta_is_cenotaph(self):
        self.assertEqual(
            decipher_ints([Tag.BODY, 1, 0, 0, 0, U64_MAX, 0, 0, 0]),
            Cenotaph(flaw=Flaw.EDICT_ASSET_ID),
        )
        self.assertEqual(
            decipher_ints([Tag.BODY, 1, 1, 0, 0, 0, U64_MAX, 0, 0]),
            Cenotaph(flaw=Flaw.EDICT_ASSET_ID),
        )
        self.assertEqual(decipher_ints([Tag.BODY, 0, 0, 2, 0, U128_MAX, 1, 0, 0]), Cenotaph(flaw=Flaw.EDICT_ASSET_ID))
        self.assertEqual(decipher_ints([Tag.BODY, 0, 0, 2, 0, 1, U128_MAX, 0, 0]), Cenotaph(flaw=Flaw.EDICT_ASSET_ID))

    def test_edict_output_over_max_is_cenotaph(self):
        self.assertEqual(decipher_ints([Tag.BODY, 0, 0, 2, 2]), Cenotaph(flaw=Flaw.EDICT_OUTPUT))
        self.assertEqual(decipher_ints([Tag.BODY, 0, 0, 1, U32_MAX + 1]), Cenotaph(flaw=Flaw.EDICT_OUTPUT))

    def test_edict_output_equal_to_len_is_allowed(self):
        # output == number of outputs means "split among non-OP_RETURN outputs"
        self.assertEqual(decipher_ints([Tag.BODY, 0, 0, 2, 1]), Envelope(edicts=[orchid_edict(2, 1)]))

    def test_trailing_integers_in_body_is_cenotaph(self):
        ints = [Tag.BODY, 0, 0, 2, 0]
        for i in range(4):
            if i == 0:
                self.assertEqual(decipher_ints(ints), Envelope(edicts=[orchid_edict(2, 0)]))
            else:
                self.assertEqual(decipher_ints(ints), Cenotaph(flaw=Flaw.TRAILING_INTEGERS))
            ints.append(0)

    def test_edict_flaw_with_land_keeps_land(self):
        self.assertEqual(
            decipher_ints([Tag.LAND, 0, Tag.BODY, 0, 0, 2, 5]),
            Cenotaph(flaw=Flaw.EDICT_OUTPUT, land=True),
        )

    def test_full_envelope(self):
        self.assertEqual(
            decipher_ints([Tag.LAND, 0, Tag.POINTER, 2, Tag.BODY, 0, 0, 250, 1], n_extra_outputs=2),
            Envelope(edicts=[orchid_edict(250, 1)], pointer=2, land=True),
        )


class EncipherTests(unittest.TestCase):
    def roundtrip(self, envelope: Envelope, n_outputs: int = 1):
        script = encipher(envelope)
        outputs = [FakeOut(script)] + [FakeOut(b"") for _ in range(n_outputs - 1)]
        expected = Envelope(
            edicts=sorted(envelope.edicts, key=lambda e: e.id),
            pointer=envelope.pointer,
            land=envelope.land,
        )
        self.assertEqual(decipher(FakeTx(outputs)), expected)

    def test_empty(self):
        self.assertEqual(encipher(Envelope()), MAGIC)
        self.roundtrip(Envelope())

    def test_land_only_bytes(self):
        # OP_RETURN OP_PUSHNUM_15 PUSH(28 0)
        self.assertEqual(encipher(Envelope(land=True)), bytes([0x6A, 0x5F, 0x02, 28, 0]))
        self.roundtrip(Envelope(land=True))

    def test_land_with_pointer_bytes(self):
        self.assertEqual(
            encipher(Envelope(pointer=1, land=True)),
            bytes([0x6A, 0x5F, 0x04, 28, 0, 22, 1]),
        )
        self.roundtrip(Envelope(pointer=1, land=True), n_outputs=2)

    def test_single_orchid_edict_bytes(self):
        # BODY, then (0, 0, 250, 1): 250 encodes as fa 01
        self.assertEqual(
            encipher(Envelope(edicts=[orchid_edict(250, 1)])),
            bytes([0x6A, 0x5F, 0x06, 0, 0, 0, 0xFA, 0x01, 1]),
        )
        self.roundtrip(Envelope(edicts=[orchid_edict(250, 1)]), n_outputs=2)

    def test_full(self):
        self.roundtrip(
            Envelope(
                edicts=[Edict(AssetId(2, 3), 1, 0), Edict(AssetId(5, 6), 4, 1), orchid_edict(7, 2)],
                pointer=3,
                land=True,
            ),
            n_outputs=4,
        )

    def test_edict_sorting(self):
        self.roundtrip(
            Envelope(edicts=[Edict(AssetId(5, 6), 4, 0), Edict(AssetId(2, 3), 1, 0)])
        )

    def test_large_payload_uses_pushdata(self):
        edicts = [Edict(AssetId(i + 1, 0), U128_MAX, 0) for i in range(20)]
        self.roundtrip(Envelope(edicts=edicts))

    def test_amount_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            encipher(Envelope(edicts=[orchid_edict(U128_MAX + 1, 0)]))


class BuilderTests(unittest.TestCase):
    def test_land_script(self):
        self.assertEqual(land_script(), encipher(Envelope(land=True)))
        self.assertEqual(land_script(pointer=1), encipher(Envelope(pointer=1, land=True)))

    def test_send_script(self):
        self.assertEqual(
            send_script([(250, 1), (10, 2)], change=3),
            encipher(Envelope(edicts=[orchid_edict(250, 1), orchid_edict(10, 2)], pointer=3)),
        )

    def test_send_script_with_auto_land(self):
        tx = FakeTx([FakeOut(send_script([(250, 1)], change=2, land=True)), FakeOut(b""), FakeOut(b"")])
        self.assertEqual(decipher(tx), Envelope(edicts=[orchid_edict(250, 1)], pointer=2, land=True))


if __name__ == "__main__":
    unittest.main()
