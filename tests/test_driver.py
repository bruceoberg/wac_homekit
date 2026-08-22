"""The bridge's pure decisions.

Only the ones that are pure. The poll loop, the accessory building and the
discovery watch all need a real device and a real Home app; a HAP-python test
harness would only be testing HAP-python.
"""

from __future__ import annotations  # Forward refs without quotes

from pathlib import Path

import pytest

from wac_homekit.driver import (
	PERSIST_DIR_NAME,
	XHM_PREFIX,
	PathPersistResolve,
	StrBase36,
	StrXhmUri,
)

# Category 2 is HomeKit's bridge category, which is what this bridge is.

CATEGORY_BRIDGE = 2


class TestPathPersistResolve:
	"""Where the pairing state lands, which decides whether a blind run works.

	The whole point is that no case needs root. `pathService` is injected so
	the suite never depends on whether this machine happens to have
	/var/lib/wac-homekit — which is exactly the ambiguity the resolution
	exists to remove.
	"""

	def test_explicit_wins_over_everything(self, tmp_path: Path) -> None:
		"""--persist-dir is a hard requirement, not a preference."""

		pathGiven = tmp_path / "somewhere"
		pathService = tmp_path / "service"
		pathService.mkdir()

		assert PathPersistResolve(pathGiven, pathService=pathService) == pathGiven

	def test_explicit_is_not_checked_for_usability(self, tmp_path: Path) -> None:
		"""A bridge quietly pairing somewhere other than where it was told is
		worse than one that fails on mkdir later."""

		pathGiven = tmp_path / "does" / "not" / "exist"

		assert PathPersistResolve(pathGiven) == pathGiven

	def test_service_dir_used_when_writable(self, tmp_path: Path) -> None:
		"""systemd's StateDirectory exists before the unit runs, which is the
		signal that this is the service rather than a person at a terminal."""

		pathService = tmp_path / "service"
		pathService.mkdir()

		assert PathPersistResolve(None, pathService=pathService) == pathService

	def test_missing_service_dir_falls_back(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		pathState = tmp_path / "state"
		monkeypatch.setenv("XDG_STATE_HOME", str(pathState))

		pathResolved = PathPersistResolve(None, pathService=tmp_path / "absent")

		assert pathResolved == pathState / PERSIST_DIR_NAME

	def test_unwritable_service_dir_falls_back(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""Present but not writable is the unprivileged case on a machine that
		once ran the service as root — no more use than absent."""

		pathService = tmp_path / "service"
		pathService.mkdir(mode=0o500)
		monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

		pathResolved = PathPersistResolve(None, pathService=pathService)

		assert pathResolved == tmp_path / "state" / PERSIST_DIR_NAME

	def test_unset_xdg_state_home_uses_the_documented_default(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		monkeypatch.delenv("XDG_STATE_HOME", raising=False)
		monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

		pathResolved = PathPersistResolve(None, pathService=tmp_path / "absent")

		assert pathResolved == tmp_path / ".local" / "state" / PERSIST_DIR_NAME

	def test_empty_xdg_state_home_is_treated_as_unset(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""XDG's own rule, and the difference between a home-relative path and
		a state directory at the filesystem root."""

		monkeypatch.setenv("XDG_STATE_HOME", "")
		monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

		pathResolved = PathPersistResolve(None, pathService=tmp_path / "absent")

		assert pathResolved == tmp_path / ".local" / "state" / PERSIST_DIR_NAME


class TestStrBase36:
	"""Base 36 is only here because HAP writes the setup payload in it.

	Round-tripped through `int(s, 36)` rather than against a table of
	expected strings: the standard library already knows how to read base 36,
	so it makes a better oracle than a second hand-written encoder would.
	"""

	@pytest.mark.parametrize("nValue", [0, 1, 9, 10, 35, 36, 1295, 1296, 4615176725])
	def test_round_trips_through_int(self, nValue: int) -> None:
		assert int(StrBase36(nValue), 36) == nValue

	def test_zero_is_a_digit_not_an_empty_string(self) -> None:
		assert StrBase36(0) == "0"

	def test_digits_are_uppercase(self) -> None:
		"""Lowercase decodes fine but does not match what a controller
		expects to see printed on a label."""

		assert StrBase36(35) == "Z"


class TestStrXhmUri:
	"""The setup payload behind the pairing QR code.

	Unpacked field by field rather than compared against a fixed string,
	because a fixed string would pass just as happily with two fields
	transposed.
	"""

	PINCODE = "517-73-973"
	SETUP_ID = "ABCD"

	def NPayload(self, strUri: str) -> int:
		strPayload = strUri[len(XHM_PREFIX):-len(self.SETUP_ID)]

		return int(strPayload, 36)

	def StrUri(self) -> str:
		return StrXhmUri(CATEGORY_BRIDGE, self.PINCODE, self.SETUP_ID)

	def test_shape(self) -> None:
		strUri = self.StrUri()

		assert strUri.startswith(XHM_PREFIX)
		assert strUri.endswith(self.SETUP_ID)
		assert len(strUri) == len(XHM_PREFIX) + 9 + len(self.SETUP_ID)

	def test_carries_the_setup_code_in_the_low_27_bits(self) -> None:
		"""Eight digits top out at 99,999,999, which is inside 27 bits — so a
		setup code never runs into the flags nibble above it."""

		assert self.NPayload(self.StrUri()) & 0x7FFFFFF == 51773973

	def test_carries_the_flags_and_category(self) -> None:
		nPayload = self.NPayload(self.StrUri())

		assert (nPayload >> 27) & 0xF == 2                  # reachable over IP
		assert (nPayload >> 31) & 0xFF == CATEGORY_BRIDGE

	def test_separators_in_the_pincode_are_ignored(self) -> None:
		"""The 3-2-3 grouping is what SRP hashes; the payload wants the digits."""

		assert StrXhmUri(CATEGORY_BRIDGE, "517-73-973", "AB") == StrXhmUri(
			CATEGORY_BRIDGE, "51773973", "AB"
		)

	def test_payload_is_padded_to_nine_digits(self) -> None:
		"""The padding is never decorative: the smallest payload a real bridge
		can produce is six base-36 digits, so the width comes from the zeros."""

		strUri = StrXhmUri(0, "00000001", "ZZZZ")

		assert strUri == f"{XHM_PREFIX}0004FTI4HZZZZ"
