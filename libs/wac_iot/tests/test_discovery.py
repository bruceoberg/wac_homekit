"""mDNS TXT record parsing.

Everything here is pure — no Zeroconf, no network.

Host names and MACs below are synthetic. The *prefixes* are real product
families, because the parser exists precisely to not care which one it sees;
the MAC tails are made up so no particular installation is baked into the
suite.
"""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_iot import DiscoFromTxt, StrTryMacSuffix
from wac_iot.discovery import MpStrTxtNormalize

# A synthetic MAC tail, used throughout.

MAC_TAIL = "0A1B2C"
HOST = f"WAC_CS_{MAC_TAIL}.local."

# What Zeroconf hands over: bytes keys and values, with literal spaces in the
# keys the device uses. The Protocol value is the one real constant here —
# hardware does report exactly this string.

g_mpBytes: dict[bytes, bytes] = {
	b"Firmware Ver": b"01.02.0003",
	b"Protocol": b"com.waclighting.strut",
	b"Protocol Ver": b"1.40",
	b"MAC": b"AABBCC0A1B2C",
}

# What a Home Assistant integration hands over: plain str.

g_mpStr: dict[str, str] = {
	"Firmware Ver": "01.02.0003",
	"Protocol": "com.waclighting.strut",
	"Protocol Ver": "1.40",
	"MAC": "AABBCC0A1B2C",
}


class TestMpStrTxtNormalize:
	def test_decodes_bytes(self) -> None:
		assert MpStrTxtNormalize(g_mpBytes) == g_mpStr

	def test_passes_str_through(self) -> None:
		assert MpStrTxtNormalize(g_mpStr) == g_mpStr

	def test_none_value_becomes_empty(self) -> None:
		"""Zeroconf represents a valueless TXT key as None."""

		assert MpStrTxtNormalize({b"Flag": None}) == {"Flag": ""}

	def test_undecodable_bytes_do_not_raise(self) -> None:
		mpStr = MpStrTxtNormalize({b"MAC": b"\xff\xfe"})

		assert "MAC" in mpStr


class TestStrTryMacSuffix:
	@pytest.mark.parametrize(
		"strHost",
		[
			f"WAC_CS_{MAC_TAIL}",
			f"WAC_CS_{MAC_TAIL}.local.",
			f"WAC_CS_{MAC_TAIL}.local",
			f"WAC_CS_{MAC_TAIL}._easylink._tcp.local.",
		],
	)
	def test_parses_suffix(self, strHost: str) -> None:
		assert StrTryMacSuffix(strHost) == MAC_TAIL

	@pytest.mark.parametrize(
		"strPrefix",
		[
			"WAC_CS",     # ColorScaping transformer
			"WAC_WCT",    # InvisiLED wall station
			"STRUT",      # the spelling the vendor document claims
			"SOME_NEW_PRODUCT_LINE",
		],
	)
	def test_prefix_is_irrelevant(self, strPrefix: str) -> None:
		"""The prefix varies by product and is not worth matching on.

		Only the trailing MAC tail is stable, so the parser anchors there.
		"""

		assert StrTryMacSuffix(f"{strPrefix}_{MAC_TAIL}.local.") == MAC_TAIL

	@pytest.mark.parametrize(
		"strHost",
		[
			"somethingelse.local.",
			"WAC_CS_",
			"",
			"nounderscore",
			"WAC_CS_NOTHEX",   # right length, not hexadecimal
			"WAC_CS_0A1B",     # too short for a MAC tail
			f"_{MAC_TAIL}",    # no prefix at all
		],
	)
	def test_returns_none_rather_than_guessing(self, strHost: str) -> None:
		assert StrTryMacSuffix(strHost) is None

	def test_hex_case_is_preserved(self) -> None:
		assert StrTryMacSuffix("wac_cs_0a1b2c.local.") == "0a1b2c"


class TestDiscoFromTxt:
	def test_parses_bytes_txt(self) -> None:
		disco = DiscoFromTxt(
			g_mpBytes,
			strHost=f"WAC_CS_{MAC_TAIL}._easylink._tcp.local.",
			strIp="10.0.0.8",
			nPort=443,
		)

		assert disco.strFirmwareVer == "01.02.0003"
		assert disco.strProtocol == "com.waclighting.strut"
		assert disco.strProtocolVer == "1.40"
		assert disco.strMac == "AABBCC0A1B2C"
		assert disco.strMacSuffix == MAC_TAIL
		assert disco.strIp == "10.0.0.8"
		assert disco.nPort == 443

	def test_str_and_bytes_agree(self) -> None:
		"""The parser is the seam Home Assistant reuses; both inputs must match."""

		discoBytes = DiscoFromTxt(g_mpBytes, strHost=HOST, strIp="10.0.0.8", nPort=443)
		discoStr = DiscoFromTxt(g_mpStr, strHost=HOST, strIp="10.0.0.8", nPort=443)

		assert discoBytes == discoStr

	def test_spaced_keys_matched_regardless_of_spacing_or_case(self) -> None:
		"""Firmware revisions have not been consistent about these keys."""

		disco = DiscoFromTxt(
			{b"firmwarever": b"1.0", b"PROTOCOL VER": b"1.40"},
			strHost=HOST,
		)

		assert disco.strFirmwareVer == "1.0"
		assert disco.strProtocolVer == "1.40"

	def test_missing_keys_are_none_not_errors(self) -> None:
		disco = DiscoFromTxt({}, strHost=HOST)

		assert disco.strFirmwareVer is None
		assert disco.strProtocol is None
		assert disco.strMac is None
		assert disco.strMacSuffix == MAC_TAIL
		assert disco.mpStrTxt == {}

	def test_empty_value_reads_as_missing(self) -> None:
		disco = DiscoFromTxt({b"MAC": b""}, strHost=HOST)

		assert disco.strMac is None

	def test_unknown_keys_are_preserved(self) -> None:
		"""The documentation lists four keys; hardware is free to send more."""

		disco = DiscoFromTxt(
			{b"MAC": b"AABBCC0A1B2C", b"Undocumented": b"surprise"},
			strHost=HOST,
		)

		assert disco.mpStrTxt["Undocumented"] == "surprise"

	def test_unparseable_host_still_yields_a_result(self) -> None:
		disco = DiscoFromTxt(g_mpBytes, strHost="random.local.")

		assert disco.strMacSuffix is None
		assert disco.strMac == "AABBCC0A1B2C"
