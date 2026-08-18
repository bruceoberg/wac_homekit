"""The `set` subcommand's pure parts.

Dispatch and refusal, not I/O. `set` is the only thing in this package that
writes to hardware, so the case worth pinning is the one where a flag the
fixture cannot honor gets refused instead of quietly dropped — a write that
reports success and changes nothing is the worst outcome a hardware test can
have.
"""

from __future__ import annotations  # Forward refs without quotes

import argparse

import pytest

from wac_iot import LIGHTMODE, SStateFan, SStateLight, SStateRgbw, SStateWhite, WacValueError
from wac_iot.cli import LightmodeParse, ObjStateByShape, PrintStateMoved, TplRgbParse


class TestTplRgbParse:
	@pytest.mark.parametrize("strArg", ["255,128,0", "255 128 0", " 255, 128 ,0 "])
	def test_accepts_both_separators(self, strArg: str) -> None:
		assert TplRgbParse(strArg) == (255, 128, 0)

	@pytest.mark.parametrize("strArg", ["255,128", "255,128,0,64", "", "255,x,0"])
	def test_rejects_malformed(self, strArg: str) -> None:
		with pytest.raises(argparse.ArgumentTypeError):
			TplRgbParse(strArg)

	def test_leaves_range_to_the_builder(self) -> None:
		"""Out-of-range parses fine here; ObjStateRgbw is what refuses it."""

		assert TplRgbParse("999,0,0") == (999, 0, 0)

		with pytest.raises(WacValueError):
			ObjStateByShape(SStateRgbw(), {"tplRgb": (999, 0, 0)})


class TestLightmodeParse:
	def test_is_case_insensitive(self) -> None:
		assert LightmodeParse("hsv") is LIGHTMODE.Hsv
		assert LightmodeParse("DimToWarm") is LIGHTMODE.DimToWarm

	def test_rejects_unknown_by_name(self) -> None:
		with pytest.raises(argparse.ArgumentTypeError):
			LightmodeParse("purple")

	def test_rejects_the_unknown_member_itself(self) -> None:
		"""`Unknown` is what an unrecognized wire value resolves to, not a mode to ask for."""

		with pytest.raises(argparse.ArgumentTypeError):
			LightmodeParse("unknown")


class TestObjStateByShape:
	def test_dispatches_on_the_state_shape(self) -> None:
		assert ObjStateByShape(SStateLight(), {"nLevel": 5000}) == {"level": 5000}
		assert ObjStateByShape(SStateRgbw(), {"tplRgb": (255, 0, 0)}) == {
			"red": 255, "green": 0, "blue": 0,
		}
		assert ObjStateByShape(SStateWhite(), {"nColorTemp": 3000}) == {"mixColorTemp": 3000}
		assert ObjStateByShape(SStateFan(), {"nFanSpeed": 3}) == {"fanSpeed": 3}

	def test_prefers_the_derived_shape(self) -> None:
		"""White and RGBW both derive from SStateLight — the subclass must win."""

		assert ObjStateByShape(SStateRgbw(), {"nHue": 3333}) == {"hue": 3333}

	@pytest.mark.parametrize(
		("state", "mpStrArg", "strFlag"),
		[
			(SStateLight(), {"tplRgb": (255, 0, 0)}, "--rgb"),
			(SStateLight(), {"nColorTemp": 3000}, "--color-temp"),
			(SStateWhite(), {"nHue": 3333}, "--hue"),
			(SStateFan(), {"nLevel": 5000}, "--level"),
		],
	)
	def test_refuses_a_flag_the_shape_cannot_honor(
		self,
		state: SStateLight | SStateFan,
		mpStrArg: dict[str, object],
		strFlag: str,
	) -> None:
		with pytest.raises(WacValueError, match=strFlag):
			ObjStateByShape(state, mpStrArg)

	def test_still_enforces_the_builders_own_exclusions(self) -> None:
		"""Dispatch does not get to relax what the builder refuses."""

		with pytest.raises(WacValueError):
			ObjStateByShape(SStateRgbw(), {"nHue": 3333, "tplRgb": (255, 0, 0)})


class TestPrintStateMoved:
	def test_reports_fields_the_request_never_mentioned(
		self,
		capsys: pytest.CaptureFixture[str],
	) -> None:
		"""Writing RGB moves hue and saturation. That is the whole point."""

		PrintStateMoved(
			{"hue": 6666, "saturation": 10000, "level": 9981},
			{"hue": 0, "saturation": 0, "level": 9981},
		)

		strOut = capsys.readouterr().out

		assert "hue: 6666 -> 0" in strOut
		assert "saturation: 10000 -> 0" in strOut
		assert "level" not in strOut

	def test_says_so_when_nothing_moved(self, capsys: pytest.CaptureFixture[str]) -> None:
		PrintStateMoved({"level": 5000}, {"level": 5000})

		assert "nothing" in capsys.readouterr().out

	def test_reports_a_field_that_appeared(self, capsys: pytest.CaptureFixture[str]) -> None:
		"""Pruned state omits None, so a field arriving looks like an added key."""

		PrintStateMoved({}, {"mixColorTemp": 3000})

		assert "mixColorTemp: None -> 3000" in capsys.readouterr().out
