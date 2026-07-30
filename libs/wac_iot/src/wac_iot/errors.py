#!/usr/bin/env python3
"""Device status codes and the exception hierarchy raised on top of them.

The device answers almost every request with HTTP 200 and reports real
success or failure in a `result` field inside the body, so the interesting
error handling happens after the HTTP layer has already declared victory.
`ErrFromResponse` is the pure function that turns a decoded body into an
exception (or `None`); `transport` is what actually raises it.
"""

from __future__ import annotations  # Forward refs without quotes

from enum import IntEnum
from typing import Any


class RESULT(IntEnum):  # tag = result
	"""Result codes the device reports in a response's `result` field."""

	Success                    = 0
	GeneralFailure             = -1
	InvalidAddr                = -2
	InvalidInput               = -3
	InvalidSize                = -4
	StorageError               = -5
	ScmCommError               = -6
	InvalidParam               = -7
	InvalidFixture             = -8
	InvalidProv                = -9
	NotEnabled                 = -10
	MaxEnabled                 = -11
	Overflow                   = -12
	MaxDevices                 = -13
	MaxPairings                = -14
	NotFound                   = -15
	AlreadyExists              = -16
	AlreadyPaired              = -17
	NameTooLarge               = -18
	InvalidAction              = -19
	AutomationFull             = -20
	InvalidFixtureAddr         = -21
	GroupAddrInvalid           = -22
	GroupIndexInvalid          = -23
	SceneAddrInvalid           = -24
	SceneIndexInvalid          = -25
	MaxIntegrations            = -26
	OutOfMemory                = -27
	AddressExists              = -28
	NvsGetError                = -29
	NvsSetError                = -30
	NvsEraseError              = -31
	SizeMismatch               = -32
	InvalidType                = -33
	CreateJsonError            = -34
	MissType                   = -35
	MissFixtures               = -36
	MissGroups                 = -37
	MissOccSensor              = -38
	AutomResFail               = -39
	CantRemoveIntegratedDevice = -40
	MaxGroups                  = -41
	NoMapping                  = -42
	MissingName                = -43
	MissingRequiredParam       = -44
	MissConfig                 = -48
	MissPayload                = -50
	MissRequestId              = -51
	MissTimestamp              = -52
	MissAction                 = -53
	MissState                  = -54
	MissTune                   = -55

	# Network

	ScanInProgress             = -60
	ScanNotStarted             = -61
	WifiOff                    = -62
	AwsNotConnected            = -63
	AwsCloudFailure            = -64
	AwsTimedOut                = -65
	AwsAlreadyCommissioned     = -66
	AwsNotCommissioned         = -67

	# Batch OTA

	ChecksumError              = -70
	GetImageError              = -71
	WriteImageError            = -72
	SubtypeError               = -73

	# DMX

	MissEnabled                = -80
	MissMapping                = -81
	MissThing                  = -82

	# Basic OTA

	OtaUpdateNotStarted        = -102
	OtaValidationFailed        = -103
	OtaEndFailed               = -104
	OtaSameImage               = -105
	OtaSetBootFailed           = -106
	OtaUpdateInProgress        = -107
	OtaPartitionFailed         = -108
	OtaImageSize               = -109
	OtaBeginFailed             = -110
	OtaInvalidParams           = -111
	OtaWriteFailed             = -112


class WacError(Exception):
	"""Base for every error this library raises."""


class WacTransportError(WacError):
	"""The request never produced a usable HTTP response.

	Connection refused, TLS handshake failure, or a non-200 HTTP status.
	"""

	def __init__(self, strMessage: str, *, nStatus: int | None = None) -> None:
		super().__init__(strMessage)
		self.nStatus = nStatus  # HTTP status, when there was one


class WacTimeoutError(WacTransportError):
	"""The device did not answer in time."""


class WacResponseError(WacError):
	"""The device answered, but the body was not something we can read.

	Undecodable JSON, a non-object payload, or a missing / unparseable
	`result` field.
	"""


class WacDeviceError(WacError):
	"""The device understood the request and refused it.

	`result` is None when the device reported a code this library does not
	know about — `nResult` always carries the raw value either way.
	"""

	def __init__(
		self,
		nResult: int,
		*,
		result: RESULT | None = None,
		strStatus: str | None = None,
	) -> None:
		strName = result.name if result is not None else f"unknown code {nResult}"
		strMessage = f"device reported {strName} ({nResult})"

		if strStatus:
			strMessage = f"{strMessage}: {strStatus}"

		super().__init__(strMessage)

		self.nResult = nResult      # raw numeric result, known code or not
		self.result = result        # resolved code, None if unrecognized
		self.strStatus = strStatus  # device's diagnostic string, when supplied


def NTryFromAny(anyValue: Any) -> int | None:
	"""Coerce a raw `result` value to an int.

	The field is documented as a string and observed as both `"0"` and `0`,
	so accept either. Returns None if it is neither.
	"""

	if isinstance(anyValue, bool):
		# bool is an int subclass; a boolean result is not a status code.

		return None

	if isinstance(anyValue, int):
		return anyValue

	if isinstance(anyValue, str):
		try:
			return int(anyValue.strip())
		except ValueError:
			return None

	return None


def ResultFromAny(anyValue: Any) -> RESULT | None:
	"""Resolve a raw `result` value to a known code, or None."""

	nResult = NTryFromAny(anyValue)

	if nResult is None:
		return None

	try:
		return RESULT(nResult)
	except ValueError:
		return None


def ErrFromResponse(obj: Any) -> WacError | None:
	"""Map a decoded response body to an exception, or None on success.

	Pure — raising is the caller's job. This is the seam worth testing:
	string-vs-numeric results, unknown codes, and the optional diagnostic
	string all get decided here.
	"""

	if not isinstance(obj, dict):
		return WacResponseError(f"expected a JSON object, got {type(obj).__name__}")

	if "result" not in obj:
		return WacResponseError("response has no 'result' field")

	nResult = NTryFromAny(obj["result"])

	if nResult is None:
		return WacResponseError(f"could not read 'result' value {obj['result']!r}")

	if nResult == RESULT.Success:
		return None

	strStatus = obj.get("status")

	return WacDeviceError(
		nResult,
		result=ResultFromAny(nResult),
		strStatus=strStatus if isinstance(strStatus, str) else None,
	)


def ErrFromStatus(nStatus: int) -> WacError | None:
	"""Map an HTTP status to an exception, or None when it is a success.

	The device documents 400 / 408 / 500 alongside 200, but it is an
	ESP32-class embedded server — treat anything outside 2xx as a transport
	failure rather than trusting that list to be exhaustive.
	"""

	if 200 <= nStatus < 300:
		return None

	if nStatus == 408:
		return WacTimeoutError("device timed out receiving the request", nStatus=nStatus)

	return WacTransportError(f"device returned HTTP {nStatus}", nStatus=nStatus)
