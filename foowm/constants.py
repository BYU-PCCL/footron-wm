from .types import NetAtom, WmAtom, ClientType, DisplayLayout

WM_NAME = "foowm"

OFFSCREEN_SOURCE_WINDOW_NAME = "FOOTRON_SOURCE_WINDOW"
PLACARD_WINDOW_NAME = "FOOTRON_PLACARD"
# A list of regex patterns for titles of windows that we want to dump offscreen
# because we don't have a more elegant way to get rid of them.
OFFSCREEN_HACK_WINDOW_PATTERNS = [
    # Chrome screen sharing notification
    r".*is sharing (your screen|a window)\.?"
]

UTF8_STRING_ATOM = "UTF8_STRING"
SUPPORTED_NET_ATOMS = [
    NetAtom.Supported,
    NetAtom.ActiveWindow,
    NetAtom.WmName,
    NetAtom.ClientList,
    NetAtom.WmCheck,
    NetAtom.WmState,
    NetAtom.WmWindowType,
    NetAtom.WmWindowTypeDock,
    NetAtom.WmWindowTypeToolbar,
    NetAtom.WmWindowTypeMenu,
    NetAtom.WmWindowTypeSplash,
    NetAtom.WmWindowTypeDialog,
    NetAtom.WmWindowTypeUtility,
    NetAtom.WmStateModal,
    NetAtom.WmStateAbove,
    NetAtom.WmStateSticky,
]
SUPPORTED_WM_ATOMS = [
    WmAtom.DeleteWindow,
    WmAtom.Protocols,
    WmAtom.Name,
    WmAtom.State,
    WmAtom.NormalHints,
]
FLOATING_WINDOW_TYPES = [
    NetAtom.WmWindowTypeDock,
    NetAtom.WmWindowTypeToolbar,
    NetAtom.WmWindowTypeUtility,
    NetAtom.WmWindowTypeDialog,
    NetAtom.WmWindowTypeMenu,
    NetAtom.WmWindowTypeSplash,
]
FLOATING_WINDOW_STATES = [
    NetAtom.WmStateModal,
    NetAtom.WmStateAbove,
    NetAtom.WmStateSticky,
]

LAYOUT_GEOMETRY = {
    ClientType.Placard: {
        DisplayLayout.Fullscreen: lambda *, width, height, **_: (
            0,
            0,
            int(width * 0.2),
            height,
        ),
        DisplayLayout.Fit4k: (0, 0, 715, 1758),
        DisplayLayout.Production: (0, 0, 715, 1758),
    },
    ClientType.OffscreenSource: lambda *, width, geometry, **_: (
        width,
        0,
        geometry.width,
        geometry.height,
    ),
    ClientType.OffscreenHack: lambda *, height, geometry, **_: (
        0,
        height,
        geometry.width,
        geometry.height,
    ),
    None: {
        DisplayLayout.Fullscreen: lambda *, width, height, **_: (0, 0, width, height),
        DisplayLayout.Fit4k: (715, 0, 3125, 1758),
        DisplayLayout.Production: lambda *, height, **_: (978, 0, 3125, height),
    },
}