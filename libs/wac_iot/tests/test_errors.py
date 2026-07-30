"""Status code to exception mapping."""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_iot import (
	RESULT,
	ErrFromResponse,
	ResultFromAny,
	WacDeviceError,
	WacResponseError,
	WacTimeoutError,
	WacTransportError,
)
from wac_iot.errors import ErrFromStatus, NTryFromAny


class TestNTryFromAny:
	"""The device documents `result` as a string and sends it both ways."""

	@pytest.mark.parametrize(
		("objResult", "nExpected"),
		[
			("0", 0),
			(0, 0),
			("-3", -3),
			(-3, -3),
			(" -18 ", -18),
			("-112", -112),
		],
	)
	def test_accepts_string_and_numeric(self, objResult: object, nExpected: int) -> None:
		assert NTryFromAny(objResult) == nExpected

	@pytest.mark.parametrize("objResult", ["", "abc", "1.5", None, [], {}, 1.5])
	def test_rejects_unreadable(self, objResult: object) -> None:
		assert NTryFromAny(objResult) is None

	def test_bool_is_not_a_status_code(self) -> None:
		"""bool subclasses int; True must not read as result 1."""

		assert NTryFromAny(True) is None
		assert NTryFromAny(False) is None


class TestResultFromAny:
	def test_resolves_known_codes(self) -> None:
		assert ResultFromAny("0") is RESULT.Success
		assert ResultFromAny(-3) is RESULT.InvalidInput
		assert ResultFromAny("-18") is RESULT.NameTooLarge
		assert ResultFromAny(-112) is RESULT.OtaWriteFailed

	def test_unknown_code_is_none(self) -> None:
		"""Codes are sparse — -45 sits inside the documented range and is unused."""

		assert ResultFromAny(-45) is None
		assert ResultFromAny(-9999) is None

	def test_unreadable_is_none(self) -> None:
		assert ResultFromAny("nope") is None


class TestErrFromResponse:
	def test_success_is_none(self) -> None:
		assert ErrFromResponse({"action": 3, "result": "0"}) is None
		assert ErrFromResponse({"action": 3, "result": 0}) is None

	def test_known_failure(self) -> None:
		err = ErrFromResponse({"action": 1, "result": "-18"})

		assert isinstance(err, WacDeviceError)
		assert err.result is RESULT.NameTooLarge
		assert err.nResult == -18
		assert err.strStatus is None
		assert "NameTooLarge" in str(err)

	def test_unknown_code_still_raises_with_raw_value(self) -> None:
		"""An unrecognized code must not be swallowed or crash the mapping."""

		err = ErrFromResponse({"result": -45})

		assert isinstance(err, WacDeviceError)
		assert err.result is None
		assert err.nResult == -45
		assert "-45" in str(err)

	def test_status_string_is_carried(self) -> None:
		err = ErrFromResponse({"result": "-7", "status": "level out of range"})

		assert isinstance(err, WacDeviceError)
		assert err.strStatus == "level out of range"
		assert "level out of range" in str(err)

	def test_non_string_status_ignored(self) -> None:
		err = ErrFromResponse({"result": "-7", "status": 42})

		assert isinstance(err, WacDeviceError)
		assert err.strStatus is None

	def test_missing_result_is_a_response_error(self) -> None:
		assert isinstance(ErrFromResponse({"action": 3}), WacResponseError)

	def test_unreadable_result_is_a_response_error(self) -> None:
		assert isinstance(ErrFromResponse({"result": "yes"}), WacResponseError)

	@pytest.mark.parametrize("obj", [None, [], "ok", 3])
	def test_non_object_body_is_a_response_error(self, obj: object) -> None:
		assert isinstance(ErrFromResponse(obj), WacResponseError)


class TestErrFromStatus:
	def test_success_range_is_none(self) -> None:
		assert ErrFromStatus(200) is None
		assert ErrFromStatus(204) is None

	def test_timeout_is_distinguished(self) -> None:
		err = ErrFromStatus(408)

		assert isinstance(err, WacTimeoutError)
		assert err.nStatus == 408

	@pytest.mark.parametrize("nStatus", [400, 404, 500, 503])
	def test_other_failures_are_transport_errors(self, nStatus: int) -> None:
		err = ErrFromStatus(nStatus)

		assert isinstance(err, WacTransportError)
		assert not isinstance(err, WacTimeoutError)
		assert err.nStatus == nStatus
