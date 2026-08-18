"""The /fixture helpers that parse a response already in hand.

No transport, no hardware — just the shapes real firmware has been seen to
return, including the ones the vendor documentation does not mention.
"""

from __future__ import annotations  # Forward refs without quotes

from typing import Any

import pytest

from wac_iot import CFixtures


class TestObjTryStateFromControl:
	def test_returns_the_echoed_state(self) -> None:
		"""An action 4 response carries the fixture's whole post-write state."""

		obj: dict[str, Any] = {
			"action": 4,
			"result": "0",
			"staMac": "A0:B7:65:1A:7D:C0",
			"state": {"level": 2000, "online": True, "status": True},
		}

		assert CFixtures.ObjTryStateFromControl(obj) == {
			"level": 2000, "online": True, "status": True,
		}

	def test_reports_what_the_firmware_accepted(self) -> None:
		"""Out-of-range values come back clamped, not as sent — that is the point."""

		obj: dict[str, Any] = {"result": "0", "state": {"mixColorTemp": 6500}}

		objState = CFixtures.ObjTryStateFromControl(obj)

		assert objState is not None
		assert objState["mixColorTemp"] == 6500

	@pytest.mark.parametrize(
		"obj",
		[
			{"action": 4, "result": "0"},
			{"state": None},
			{"state": "ok"},
			{"state": []},
			{},
		],
	)
	def test_returns_none_without_a_usable_state(self, obj: dict[str, Any]) -> None:
		assert CFixtures.ObjTryStateFromControl(obj) is None
