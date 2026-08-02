#!/usr/bin/env python3
"""Client library for WAC Lighting IoT devices.

Everything below is the supported surface. Consumers import from here, not
from submodules — this list is what a Home Assistant integration would build
against, so treat additions as API decisions.

Values are in device units throughout. Converting them to whatever a
consumer's platform wants is that consumer's job, deliberately.
"""

from importlib.metadata import version, metadata
from pathlib import Path

# Package metadata — read from pyproject.toml at install time so there is
# exactly one place to update the version / author.
__project__ = __name__
__version__ = version(__project__)
__author_email__ = metadata(__project__)["Author-email"]

# Useful for locating data files bundled alongside the source.
g_pathCode = Path(__file__).parent

from .client import CClient, GROUP_ADDR_ALL
from .control import (
	COLOR_TEMP_LEVEL_MAX,
	COLOR_TEMP_LEVEL_MIN,
	FAN_SPEED_MAX,
	FAN_SPEED_MIN,
	HUE_MAX,
	HUE_MIN,
	LEVEL_MAX,
	LEVEL_MIN,
	RGB_MAX,
	RGB_MIN,
	SATURATION_MAX,
	SATURATION_MIN,
	ObjStateFan,
	ObjStateLight,
	ObjStateRgbw,
	ObjStateWhite,
)
from .discovery import (
	SERVICE_TYPE,
	CBrowser,
	DiscoFromTxt,
	FIsZeroconfAvailable,
	LDiscoBrowse,
	SDisco,
	StrTryMacSuffix,
)
from .device import CDevice, SDeviceInfo, SNwkState
from .errors import (
	RESULT,
	ErrFromResponse,
	ResultFromAny,
	WacDeviceError,
	WacError,
	WacResponseError,
	WacTimeoutError,
	WacTransportError,
	WacValueError,
)
from .fixture import CFixtures
from .snapshot import CSnapshot, StrNormMac
from .models import (
	FIXTUREK,
	LIGHTMODE,
	CFixture,
	SDetail,
	SDetailMotor,
	SDetailWhite,
	SState,
	SStateFan,
	SStateLight,
	SStateMotor,
	SStateRgbw,
	SStateWall,
	SStateWhite,
	STune,
	STuneMotor,
	STuneRgbw,
	STuneWhite,
)
from .transport import PORT_HTTP, PORT_HTTPS, CTransport, ProbeHost, SPortProbe, SProbe

__all__ = [
	# Entry point for nearly everything.
	"CClient",

	# Endpoints, for a consumer that wants to compose its own transport.
	"CTransport",
	"CDevice",
	"CFixtures",

	# Discovery. DiscoFromTxt is pure and Zeroconf-free — a consumer with its
	# own mDNS stack should call it directly and ignore CBrowser, which needs
	# the `discovery` extra and says so if it is missing.
	"DiscoFromTxt",
	"StrTryMacSuffix",
	"SDisco",
	"CBrowser",
	"LDiscoBrowse",
	"FIsZeroconfAvailable",
	"SERVICE_TYPE",

	# Errors. Catch WacError to catch everything this library raises.
	"WacError",
	"WacDeviceError",
	"WacTransportError",
	"WacTimeoutError",
	"WacResponseError",
	"WacValueError",
	"RESULT",
	"ErrFromResponse",
	"ResultFromAny",

	# One poll's worth of a device, and the identifiers a consumer registers
	# things under.
	"CSnapshot",
	"StrNormMac",

	# Control state, in device units. The bounds are exported because a
	# consumer converting into them should not be hardcoding 10000.
	"ObjStateLight",
	"ObjStateWhite",
	"ObjStateRgbw",
	"ObjStateFan",
	"LEVEL_MIN",
	"LEVEL_MAX",
	"HUE_MIN",
	"HUE_MAX",
	"SATURATION_MIN",
	"SATURATION_MAX",
	"RGB_MIN",
	"RGB_MAX",
	"COLOR_TEMP_LEVEL_MIN",
	"COLOR_TEMP_LEVEL_MAX",
	"FAN_SPEED_MIN",
	"FAN_SPEED_MAX",

	# Fixtures and the structures they carry.
	"CFixture",
	"FIXTUREK",
	"LIGHTMODE",
	"SState",
	"SStateLight",
	"SStateWhite",
	"SStateRgbw",
	"SStateMotor",
	"SStateWall",
	"SStateFan",
	"STune",
	"STuneWhite",
	"STuneRgbw",
	"STuneMotor",
	"SDetail",
	"SDetailWhite",
	"SDetailMotor",

	# Device information.
	"SDeviceInfo",
	"SNwkState",

	# Probing, for working out what a given device actually serves.
	"ProbeHost",
	"SProbe",
	"SPortProbe",
	"PORT_HTTP",
	"PORT_HTTPS",

	# Constants worth naming.
	"GROUP_ADDR_ALL",
]
