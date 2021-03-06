import datetime
import logging
import queue
import re
from typing import Dict, Callable, Optional, Any, List, Set

from Xlib.display import Display
from Xlib import X, error, Xatom, Xutil
from Xlib.protocol.display import Screen
from Xlib.protocol import event
from Xlib.xobject.colormap import Colormap
from Xlib.xobject.drawable import Window

from .constants import (
    WM_NAME,
    OFFSCREEN_SOURCE_WINDOW_NAME,
    PLACARD_WINDOW_NAME,
    OFFSCREEN_HACK_WINDOW_PATTERNS,
    UTF8_STRING_ATOM,
    SUPPORTED_NET_ATOMS,
    SUPPORTED_WM_ATOMS,
    FLOATING_WINDOW_TYPES,
    LAYOUT_GEOMETRY,
    FLOATING_WINDOW_STATES,
    LOADER_WINDOW_NAME,
    DEFAULT_CLEAR_TYPES,
    EXPERIENCE_VIEWPORT_WINDOW_NAME,
)
from .types import (
    Client,
    ClientType,
    WindowGeometry,
    NetAtom,
    WmAtom,
    ExtendedWMNormalHints,
    DisplayScenario,
    DisplayLayout,
)
from .util import debug_log_size_hints, debug_log_window_geometry, debug_value_change

logger = logging.getLogger(WM_NAME)


class FootronWindowManager:
    def __init__(self, display_scenario: DisplayScenario):
        self._display: Display
        self._screen: Screen
        self._root: Window
        self._check: Window
        self._experience_viewport: Window
        self._true_color_colormap: Colormap
        self._xembed_info_atom: int
        self._utf8_atom: int
        self._width: int
        self._height: int

        self.message_queue: queue.Queue = queue.Queue()
        self._layout: DisplayLayout = DisplayLayout.Full
        self._net_atoms: Dict[str, int] = {}
        self._wm_atoms: Dict[str, int] = {}
        self._event_handlers: Dict[int, Callable[[Any], None]] = {
            X.MapRequest: self._handle_map_request,
            X.UnmapNotify: self._handle_unmap_notify,
            X.ConfigureNotify: self._handle_configure_notify,
            X.ConfigureRequest: self._handle_configure_request,
            X.PropertyNotify: self._handle_property_notify,
            X.EnterNotify: self._handle_enter_notify,
        }

        self._debug_logging: bool = logging.root.level < logging.INFO

        self._display_scenario: DisplayScenario = display_scenario
        self._placard: Optional[Client] = None
        self._loader: Optional[Client] = None
        self._clients: Dict[int, Client] = {}
        self._client_parents: Set[int] = set()

    def start(self):
        self._setup()
        self._loop()

    @property
    def layout(self):
        return self._layout

    def set_layout(self, layout: DisplayLayout, after: Optional[datetime.datetime]):
        # We could use @property.setter here but PEP 8 recommends against using it
        # when the function has side effects or does anything expensive:
        # https://peps.python.org/pep-0008/#designing-for-inheritance
        # (see notes on bullet 3)
        if layout == self._layout:
            return

        self._layout = layout
        self._setup_workarea()
        self._update_viewport_geometry(after)

    @property
    def clients(self):
        return self._clients

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    @property
    def placard(self):
        return self._placard

    def _setup(self):
        logger.debug("Starting setup")

        # Based on berry's setup method in wm.c
        # (https://github.com/JLErvin/berry/blob/master/wm.c) except that we don't
        # handle input or focus because our display doesn't support mouse or touch
        # input
        self._display = Display()
        self._screen = self._display.screen()
        self._root = self._screen.root
        self._width = self._screen.width_in_pixels
        self._height = self._screen.height_in_pixels

        self._root.change_attributes(
            event_mask=X.StructureNotifyMask
            | X.SubstructureRedirectMask
            | X.SubstructureNotifyMask
            | X.ButtonPressMask
        )

        self._setup_atoms()
        self._setup_colors()
        self._setup_check_window()
        self._setup_root_window()
        self._setup_experience_viewport_window()
        self._setup_workarea()

        logger.debug("Setup finished")

    def _setup_atoms(self):
        self._utf8_atom = self._display.intern_atom(UTF8_STRING_ATOM)
        for atom_str in SUPPORTED_NET_ATOMS:
            self._net_atoms[atom_str] = self._display.intern_atom(atom_str)
        for atom_str in SUPPORTED_WM_ATOMS:
            self._wm_atoms[atom_str] = self._display.intern_atom(atom_str)
        self._xembed_info_atom = self._display.intern_atom("_XEMBED_INFO")

    def _setup_colors(self):
        try:
            self._true_color_visual_id = next(
                v
                for v in next(
                    d for d in self._display.screen().allowed_depths if d["depth"] == 32
                )["visuals"]
                if v["visual_class"] == X.TrueColor
            )["visual_id"]
        except StopIteration:
            logger.critical(
                "Couldn't find 32 bit depth X visual, can't create transparent windows",
                exc_info=True,
            )
            raise

        self._true_color_colormap = self._root.create_colormap(
            self._true_color_visual_id, X.AllocNone
        )

    def _setup_check_window(self):
        # Presumably X.CopyFromParent here will set this window to use root's depth,
        # which seems fine. If we have an issue with this somehow, we might need to
        # mess with the depth parameter, because I just guessed.
        self._check = self._root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)

        self._check.change_property(
            self._net_atoms[NetAtom.WmCheck], Xatom.WINDOW, 32, [self._check.id]
        )
        self._set_window_title(self._check, WM_NAME)

    def _setup_root_window(self):
        self._root.change_property(
            self._net_atoms[NetAtom.WmCheck], Xatom.WINDOW, 32, [self._check.id]
        )
        self._root.change_property(
            self._net_atoms[NetAtom.WorkArea],
            Xatom.CARDINAL,
            32,
            list(
                LAYOUT_GEOMETRY[None][DisplayScenario.Production](layout=self._layout)
            ),
        )
        # Set supported EWMH atoms
        self._root.change_property(
            self._net_atoms[NetAtom.Supported],
            Xatom.ATOM,
            32,
            list(self._net_atoms.values()),
        )

    def _setup_experience_viewport_window(self):
        # This exists to force all experience windows into a single window that can be
        #  captured.
        self._experience_viewport: Window = self._create_parent_window(
            *LAYOUT_GEOMETRY[None][self._display_scenario](
                layout=self._layout, width=self._width, height=self._height
            )
        )
        # Register this as a real window
        self._experience_viewport.change_property(
            self._wm_atoms[WmAtom.State],
            self._wm_atoms[WmAtom.State],
            32,
            [Xutil.NormalState],
        )
        # Can't remember what this does, but I think it's important
        self._experience_viewport.change_property(
            self._xembed_info_atom, self._xembed_info_atom, 32, [0, 1]
        )
        # Set name of experience parent
        self._set_window_title(
            self._experience_viewport, EXPERIENCE_VIEWPORT_WINDOW_NAME
        )

        self._experience_viewport.map()

    def _setup_workarea(self):
        # Chromium (at least) appears to need this to stop overscaling when going
        # fullscreen:
        # https://source.chromium.org/chromium/chromium/src/+/main:ui/base/x/x11_display_util.cc;l=58
        self._root.change_property(
            self._net_atoms[NetAtom.WorkArea],
            Xatom.CARDINAL,
            32,
            list(
                LAYOUT_GEOMETRY[None][DisplayScenario.Production](layout=self._layout)
            ),
        )

    def _create_parent_window(self, x, y, width, height) -> Window:
        return self._root.create_window(
            x,
            y,
            width,
            height,
            0,
            32,
            X.InputOutput,
            self._true_color_visual_id,
            background_pixel=0,
            border_pixel=0,
            colormap=self._true_color_colormap,
            # TODO: Not sure if we need this
            override_redirect=True,
        )

    def _update_viewport_geometry(self, after: Optional[datetime.datetime]):
        windows_resized = 0
        self._scale_experience_viewport_window(
            WindowGeometry(
                *LAYOUT_GEOMETRY[None][self._display_scenario](
                    layout=self._layout, width=self._width, height=self._height
                )
            )
        )
        for client in self._clients.values():
            if not client.in_experience_viewport:
                continue
            if after and client.created_at <= after:
                continue

            client.geometry = self._client_geometry(
                client.desired_geometry, client.type, client.floating
            )
            self.scale_client(client, client.geometry)
            windows_resized += 1
        logger.debug(f"Updated viewport geometry: resized {windows_resized} windows")
        self._preserve_window_order()

    def _raise_placard(self):
        if not self._placard:
            return

        logger.debug("Raising placard...")
        self._placard.parent.raise_window()

    def _raise_loader(self):
        if not self._loader:
            return

        logger.debug("Raising loading window...")
        self._loader.parent.raise_window()

    def _preserve_window_order(self):
        self._raise_loader()
        self._raise_placard()
        self._display.sync()

    def _handle_map_request(self, ev: event.MapRequest):
        # Background on mapping and unmapping (last paragraph):
        # https://magcius.github.io/xplain/article/x-basics.html#lets-go
        logger.debug(f"Handling MapRequest event for window with ID {ev.window.id}")

        try:
            attrs = ev.window.get_attributes()
        except error.XError:
            logger.exception(
                f"Error attempting to get attributes for window {hex(ev.window.id)}"
            )
            return

        if (
            not attrs
            or attrs.override_redirect
            or ev.window.id in self._client_parents
            or ev.window.id == self._experience_viewport
        ):
            return

        self._manage_new_window(ev.window)

    def _handle_unmap_notify(self, ev: event.UnmapNotify):
        window_id = ev.window.id
        logger.debug(f"Handling UnmapNotify event for window {hex(window_id)}")

        if window_id in self._client_parents:
            return

        try:
            client = self._clients[window_id]
        except KeyError:
            logger.debug(
                f"No client found for UnmapNotify event with ID {hex(ev.window.id)}"
            )
            return

        if client.ignore_unmaps > 0:
            logger.debug(
                f"ignore_unmaps = {client.ignore_unmaps}, ignoring unmap event for window with ID {hex(ev.window.id)}"
            )
            client.ignore_unmaps -= 1
            return

        if client.type == ClientType.Placard:
            logger.info("Placard window is closing")
            self._placard = None
        elif client.type == ClientType.Loader:
            logger.info("Loading window is closing")
            self._loader = None

        self._client_parents.remove(client.parent.id)
        client.parent.destroy()

        del self._clients[window_id]
        self._set_ewmh_clients_list()
        self._preserve_window_order()

    def _handle_configure_notify(self, ev: event.ConfigureNotify):
        logger.debug(f"Handling ConfigureNotify event for window {hex(ev.window.id)}")

        if ev.window.id == self._root.id:
            old_dimensions = (self._width, self._height)
            self._width = ev.width
            self._height = ev.height
            # TODO: If we're going to handle this for real, we should really update all
            #  of our managed non-floating windows.
            #  @vinhowe: But I can't imagine that we we'll get a lot of screen dimension
            #  updates we need to handle "hot" in production.

            if self._debug_logging:
                logger.debug("Resizing root window:")
                debug_value_change(
                    logger.debug,
                    "width, height",
                    old_dimensions,
                    (self._width, self._height),
                )
        # TODO: If we ever need more involved multi-display handling, this would be
        #  the place to do it

    def _handle_configure_request(self, ev: event.ConfigureRequest):
        logger.debug(f"Handling ConfigureRequest event for window {hex(ev.window.id)}")

        if ev.window.id in self._client_parents:
            return

        try:
            client = self._clients[ev.window.id]
        except KeyError:
            logger.debug(
                f"No client found for ConfigureRequest event with ID {hex(ev.window.id)}"
            )
            return

        # We don't let the window types that we manage set their own geometry
        # directly, but we do want to allow offscreen source windows to set their own
        # dimensions, if not position.
        desired_geometry = WindowGeometry(ev.x, ev.y, ev.width, ev.height)
        client.desired_geometry = desired_geometry
        client.geometry = self._client_geometry(
            desired_geometry, client.type, client.floating
        )

        self.scale_client(client, client.geometry)

    def _handle_property_notify(self, ev: event.PropertyNotify):
        logger.debug(f"Handling PropertyNotify event for window {hex(ev.window.id)}")

        if ev.window.id in self._client_parents:
            return

        try:
            client = self._clients[ev.window.id]
        except KeyError:
            logger.debug(
                f"No client found for PropertyNotify event with ID {hex(ev.window.id)}"
            )
            return

        if ev.state == X.PropertyDelete:
            # Not sure why berry doesn't handle PropertyDelete
            return

        if ev.atom in [self._net_atoms[NetAtom.WmName], self._wm_atoms[WmAtom.Name]]:
            logger.debug(f"Handling title update on window {hex(client.target.id)}:")

            old_title = client.title
            client.title = self._window_title(client.target)
            if self._debug_logging:
                debug_value_change(
                    logger.debug,
                    f"title of window {hex(client.target.id)}",
                    old_title,
                    client.title,
                )

            old_type = client.type
            client.type = self._client_type_from_title(client.title)
            if self._debug_logging:
                debug_value_change(
                    logger.debug,
                    f"type of window {hex(client.target.id)}",
                    old_type,
                    client.type,
                )

            if old_type == client.type:
                # Everything after this point assumes that the client type has changed
                return

            client.geometry = self._client_geometry(
                client.desired_geometry, client.type, client.floating
            )
            self.scale_client(client, client.geometry)

            if client.type in [ClientType.Placard, ClientType.OffscreenSource]:
                if client.type == ClientType.Placard:
                    self._placard = client
                # Move client outside of the experience parent viewport if that's
                # where it was
                self._reparent_parent(client, self._root)
            elif client.type in [None, ClientType.Loader]:
                if client.type == ClientType.Loader:
                    self._loader = client
                self._reparent_parent(client, self._experience_viewport)
            return

        if ev.atom == self._wm_atoms[WmAtom.NormalHints]:
            wm_normal_hints = self._extended_wm_normal_hints(client.target)
            if not wm_normal_hints:
                return

            if self._debug_logging:
                logger.debug(
                    f"Received size hints update on window {hex(client.target.id)}:"
                )
                debug_log_size_hints(logger.debug, wm_normal_hints)
                client.desired_geometry = WindowGeometry(
                    wm_normal_hints.x,
                    wm_normal_hints.y,
                    wm_normal_hints.max_width,
                    wm_normal_hints.max_height,
                )
                self._preserve_window_order()
            return

        if ev.atom == self._net_atoms[NetAtom.WmState]:
            logger.debug(f"Received _NET_WM_STATE update on {hex(client.target.id)}:")
            # If client decided to change window state, just try to force it back
            self.scale_client(client, client.geometry)
            return

    def _handle_enter_notify(self, ev: event.EnterNotify):
        logger.debug(f"Handling EnterNotify event for window {hex(ev.window.id)}")
        # Handling EnterNotify events seems to let us use popup menus in Chrome,
        # etc., which is useful at least for debugging
        self._root.change_property(
            self._net_atoms[NetAtom.ActiveWindow], Xatom.WINDOW, 32, [ev.window.id]
        )
        ev.window.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)

    def _reparent_parent(self, client: Client, new_parent: Window):
        logger.debug(
            f"Reparenting parent of client {hex(client.target.id)} to window {hex(new_parent.id)}"
        )
        client.parent.reparent(new_parent, client.geometry.x, client.geometry.y)
        self._display.sync()

    def _manage_new_window(self, window: Window):
        if window.id in self._clients:
            # TODO: Add log statement here
            # Just ignore any map requests for existing clients
            return

        window_type = window.get_property(
            self._net_atoms[NetAtom.WmWindowType], Xatom.ATOM, 0, 32
        )
        # TODO: We could probably refactor these two similar
        #  `if (condition) then floating = True` blocks into one cleaner block
        floating = False
        if window_type:
            atom_id = window_type.value[0]
            if atom_id in [self._net_atoms[atom] for atom in FLOATING_WINDOW_TYPES]:
                # TODO: Add log statement here
                logger.debug(
                    f"_NET_WM_WINDOW_TYPE on new window {hex(window.id)} matches a floating window type"
                )
                floating = True
                # An alternative is just not managing these window types with the
                # following code. The problem is that they'll all just show up in the
                # upper left hand corner of the screen, which is hardly desirable.

                # window.map()
                # return

        ewmh_state = window.get_property(
            self._net_atoms[NetAtom.WmState], Xatom.ATOM, 0, 32
        )
        if ewmh_state and any(
            self._net_atoms[atom] in ewmh_state.value
            # TODO: Add other states that would qualify here
            for atom in FLOATING_WINDOW_STATES
        ):
            logger.debug(
                f"_NET_WM_STATE on new window {hex(window.id)} matches a floating window state"
            )
            floating = True

        wm_normal_hints = self._extended_wm_normal_hints(window)
        normal_hints_geometry = None
        # Some special window types will try to position themselves by using these
        # sizing hints, which is fine as long as the window identifies itself as a
        # floating window somehow
        if wm_normal_hints:
            if self._debug_logging:
                logger.debug(f"Found size hints on new window {hex(window.id)}:")
                debug_log_size_hints(logger.debug, wm_normal_hints)
                if wm_normal_hints.flags & (Xutil.PPosition | Xutil.PMaxSize):
                    normal_hints_geometry = WindowGeometry(
                        wm_normal_hints.x,
                        wm_normal_hints.y,
                        wm_normal_hints.max_width,
                        wm_normal_hints.max_height,
                    )

        # @vinhowe: At this point, berry gets class hint information from the window,
        # but it's unclear to me that it ever does anything with that information

        try:
            x_geometry = window.get_geometry()
        except error.BadDrawable:
            logger.exception(
                f"Error while attempting to get window geometry for new window {hex(window.id)}"
            )
            return

        # TODO: Should we be doing error handling here (map has onerror arg, set to
        #  None by default)?
        window.change_attributes(
            event_mask=X.EnterWindowMask
            | X.FocusChangeMask
            | X.PropertyChangeMask
            | X.StructureNotifyMask
        )

        # This sets WM_STATE on our managed client to NormalState, which is required
        # by Chromium for a window to be registered as a capture source on Linux:
        # https://source.chromium.org/chromium/_/webrtc/src.git/+/a1aa9d732cf08644a898dd9d93fc50c849cd83d4:modules/desktop_capture/linux/window_list_utils.cc;l=43-45,48-50
        # berry doesn't set this property for some reason, but dwm (which berry looks
        # to be based on) does.
        window.change_property(
            self._wm_atoms[WmAtom.State],
            self._wm_atoms[WmAtom.State],
            32,
            [Xutil.NormalState],
        )

        title = self._window_title(window)
        if title:
            logger.debug(f"Title for new window {hex(window.id)} is {title}")
        else:
            logger.debug(f"No title for new window {hex(window.id)}")

        client_type = FootronWindowManager._client_type_from_title(title)
        logger.debug(f"Client type for new window {hex(window.id)} is {client_type}")

        # If a window is matched with a specific type, we don't want to let it set
        # its own dimensions
        floating = floating and client_type is None
        logger.debug(
            f"Floating state for new window {hex(window.id)}: {str(floating).lower()}"
        )

        desired_geometry = (
            normal_hints_geometry
            if normal_hints_geometry
            else WindowGeometry(
                x_geometry.x, x_geometry.y, x_geometry.width, x_geometry.height
            )
        )
        if self._debug_logging:
            logger.debug(f"Desired geometry for new window {hex(window.id)}:")
            debug_log_window_geometry(logger.debug, desired_geometry)

        geometry = self._client_geometry(desired_geometry, client_type, floating)
        if self._debug_logging:
            logger.debug(f"Actual geometry for new window {hex(window.id)}:")
            debug_log_window_geometry(logger.debug, geometry)

        window.change_property(
            self._xembed_info_atom, self._xembed_info_atom, 32, [0, 1]
        )
        window.change_attributes(override_redirect=True)

        parent = self._create_parent_window(0, 0, 1, 1)
        window.reparent(parent, 0, 0)
        parent.map()
        self._client_parents.add(parent.id)
        logger.debug(f"ID of parent for window {hex(window.id)} is {hex(parent.id)}")
        self._display.sync()

        client = Client(
            window,
            parent,
            geometry,
            desired_geometry,
            title,
            client_type,
            floating,
            datetime.datetime.now(),
        )

        if client_type in [None, ClientType.Loader]:
            self._reparent_parent(client, self._experience_viewport)

        if client_type == ClientType.Placard:
            logger.info("Matched new placard window")
            self._placard = client
        elif client_type == ClientType.Loader:
            logger.info("Matched new loading window")
            self._loader = client

        self.scale_client(client, client.geometry)
        window.map()
        self._preserve_window_order()
        self._clients[client.target.id] = client
        self._set_ewmh_clients_list()

    def clear_viewport(
        self, before: datetime.datetime, include: Optional[List[ClientType]]
    ):
        windows_killed = 0
        windows_skipped = 0
        clear_types = include if include is not None else DEFAULT_CLEAR_TYPES
        if not include:
            logger.debug(f"Attempting to clear viewport using default include list")
        else:
            logger.debug(
                f"Attempting to clear viewport using custom include list: {include}"
            )
        for client in list(self._clients.values()):
            if client.type not in clear_types:
                continue
            if client.created_at > before:
                windows_skipped += 1
                continue

            # Killed client will request unmap, which will kill parent if one exists
            client.target.kill_client()
            windows_killed += 1
        logger.debug(
            f"Cleared viewport: killed {windows_killed} window(s) and skipped {windows_skipped} window(s)"
        )
        self._set_ewmh_clients_list()
        self._preserve_window_order()

    @staticmethod
    def _client_type_from_title(title: Optional[str]) -> Optional[ClientType]:
        if not title:
            return None

        if any(re.match(pattern, title) for pattern in OFFSCREEN_HACK_WINDOW_PATTERNS):
            return ClientType.OffscreenHack

        if OFFSCREEN_SOURCE_WINDOW_NAME in title:
            return ClientType.OffscreenSource

        if PLACARD_WINDOW_NAME in title:
            return ClientType.Placard

        if LOADER_WINDOW_NAME in title:
            return ClientType.Loader

        return None

    @staticmethod
    def _extended_wm_normal_hints(window: Window):
        # noinspection PyProtectedMember
        # Again, as stated in the comment above ExtendedWMNormalHints, we build our
        # own struct because we want access to x and y
        return window._get_struct_prop(
            Xatom.WM_NORMAL_HINTS, Xatom.WM_SIZE_HINTS, ExtendedWMNormalHints
        )

    def _client_geometry(
        self,
        desired_geometry: WindowGeometry,
        client_type: Optional[ClientType],
        floating=False,
    ):
        if client_type is None and floating:
            return desired_geometry

        geometry_factory = LAYOUT_GEOMETRY[client_type]

        if isinstance(geometry_factory, dict):
            geometry_factory = geometry_factory[self._display_scenario]

        if isinstance(geometry_factory, tuple):
            return WindowGeometry(*geometry_factory)

        return WindowGeometry(
            *geometry_factory(
                width=self._width,
                height=self._height,
                geometry=desired_geometry,
                layout=self._layout,
            )
        )

    def _set_ewmh_clients_list(self):
        # TODO: Unclear if this list should include parents? I'm guessing not since
        #  they don't really provide that much information.
        new_clients = map(lambda a: a.target.id, self._clients.values())
        logger.debug(
            f"Updating _NET_CLIENT_LIST on root window: {list(map(hex, new_clients))}"
        )

        self._root.change_property(
            self._net_atoms[NetAtom.ClientList], Xatom.WINDOW, 32, list(new_clients)
        )

    def _scale_experience_viewport_window(self, geometry: WindowGeometry):
        parent_id = self._experience_viewport.id
        if self._debug_logging:
            logger.debug(
                f"Attempting to experience parent (id {hex(parent_id)}) to new geometry:"
            )
            debug_log_window_geometry(logger.debug, geometry)

        try:
            self._experience_viewport.configure(
                x=geometry.x,
                y=geometry.y,
                width=max(geometry.width, 1),
                height=max(geometry.height, 1),
            )
        except Exception:
            logger.exception(f"Error while scaling experience parent to {geometry}")

    def scale_client(self, client, geometry: WindowGeometry):
        if self._debug_logging:
            logger.debug(
                f"Attempting to scale window {hex(client.target.id)} to new geometry:"
            )
            debug_log_window_geometry(logger.debug, geometry)

        in_experience_viewport = client.in_experience_viewport
        width = max(geometry.width, 1)
        height = max(geometry.height, 1)
        try:
            client.parent.configure(
                x=0 if in_experience_viewport else geometry.x,
                y=0 if in_experience_viewport else geometry.y,
                width=width,
                height=height,
            )
            client.target.configure(
                x=0,
                y=0,
                width=width,
                height=height,
            )
            # Let preserve_window_order handle display syncing so we don't get any
            # flickering
            self._preserve_window_order()
        except Exception:
            logger.exception(
                f"Error while scaling client {hex(client.target.id)} to {geometry}"
            )

    def _window_title(self, window: Window):
        """Simplify dealing with _NET_WM_NAME (UTF-8) vs. WM_NAME (legacy)"""
        try:
            title = None
            for atom in (self._net_atoms[NetAtom.WmName], self._wm_atoms[WmAtom.Name]):
                try:
                    title = window.get_full_property(atom, 0)
                except UnicodeDecodeError:
                    title = None
                else:
                    if title:
                        title = title.value
                        if isinstance(title, bytes):
                            title = title.decode("latin1", "replace")
                        return title

            return title
        except error.XError:
            logger.exception("Error while getting window title")
            return None

    def _set_window_title(self, window: Window, title: str):
        """Set the window title to the given string"""
        if self._debug_logging:
            logger.debug(f"Setting title of window {hex(window.id)}to {title}")

        try:
            # Set both _NET_WM_NAME and the older WM_NAME
            for atom in (self._net_atoms[NetAtom.WmName], self._wm_atoms[WmAtom.Name]):
                window.change_text_property(atom, self._utf8_atom, title)
        except Exception:
            logger.exception(
                f"Error while setting title of window {hex(window.id)} to {title}"
            )

    def _handle_message(self, message: Dict):
        message_type = message["type"]
        logging.debug(f"Processing message of type '{message_type}'")
        if message_type == "layout":
            if "layout" not in message:
                logger.error("Required 'layout' parameter not in message")
                return

            layout = message["layout"]
            if not isinstance(layout, str):
                logger.error("Parameter 'layout' should be a string")
                return

            after = message["after"] if "after" in message else None
            if after is not None and not isinstance(after, int):
                logger.error("Parameter 'after' should be an int")
                return

            self.set_layout(
                DisplayLayout(layout),
                after=datetime.datetime.fromtimestamp(after / 1000)
                if after is not None
                else None,
            )
            return

        if message_type == "clear_viewport":
            if "before" not in message:
                logger.error("Required 'before' parameter not in message")
                return

            before = message["before"]
            if not isinstance(before, int):
                logger.error("Parameter 'before' should be an int")
                return
            include = message["include"] if "include" in message else None
            self.clear_viewport(
                datetime.datetime.fromtimestamp(before / 1000),
                list(map(ClientType, include)) if include is not None else None,
            )
            return

        logger.error(f"Unhandled message type '{message_type}'")

    def _process_messages(self):
        while True:
            try:
                self._handle_message(self.message_queue.get_nowait())
            except queue.Empty:
                break

    def _loop(self):
        while True:
            ev = self._display.next_event()
            if ev.type not in self._event_handlers:
                logger.debug(
                    f"Received {ev.__class__.__name__} event with no configured handler"
                )
                continue

            logger.debug(f"Handling event of type {ev.__class__.__name__}")
            self._event_handlers[ev.type](ev)
            self._process_messages()
