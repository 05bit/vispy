# -*- coding: utf-8 -*-
# Copyright (c) 2015, Vispy Development Team.
# Distributed under the (new) BSD License. See LICENSE.txt for more info.

from __future__ import division

import weakref
import struct

from .. import gloo
from .. import app
from .node import Node
from .visuals import VisualNode
from ..visuals.transforms import STTransform, TransformCache, TransformSystem
from ..color import Color
from ..util import logger
from ..util.profiler import Profiler
from .subscene import SubScene
from .events import SceneMouseEvent
from .widgets import Widget


class SceneCanvas(app.Canvas):
    """A Canvas that automatically draws the contents of a scene

    Parameters
    ----------
    title : str
        The widget title
    size : (width, height)
        The size of the window.
    position : (x, y)
        The position of the window in screen coordinates.
    show : bool
        Whether to show the widget immediately. Default False.
    autoswap : bool
        Whether to swap the buffers automatically after a draw event.
        Default True. If True, the ``swap_buffers`` Canvas method will
        be called last (by default) by the ``canvas.draw`` event handler.
    app : Application | str
        Give vispy Application instance to use as a backend.
        (vispy.app is used by default.) If str, then an application
        using the chosen backend (e.g., 'pyglet') will be created.
        Note the canvas application can be accessed at ``canvas.app``.
    create_native : bool
        Whether to create the widget immediately. Default True.
    vsync : bool
        Enable vertical synchronization.
    resizable : bool
        Allow the window to be resized.
    decorate : bool
        Decorate the window. Default True.
    fullscreen : bool | int
        If False, windowed mode is used (default). If True, the default
        monitor is used. If int, the given monitor number is used.
    config : dict
        A dict with OpenGL configuration options, which is combined
        with the default configuration options and used to initialize
        the context. See ``canvas.context.config`` for possible
        options.
    shared : Canvas | GLContext | None
        An existing canvas or context to share OpenGL objects with.
    keys : str | dict | None
        Default key mapping to use. If 'interactive', escape and F11 will
        close the canvas and toggle full-screen mode, respectively.
        If dict, maps keys to functions. If dict values are strings,
        they are assumed to be ``Canvas`` methods, otherwise they should
        be callable.
    parent : widget-object
        The parent widget if this makes sense for the used backend.
    dpi : float | None
        Resolution in dots-per-inch to use for the canvas. If dpi is None,
        then the value will be determined by querying the global config first,
        and then the operating system.
    always_on_top : bool
        If True, try to create the window in always-on-top mode.
    px_scale : int > 0
        A scale factor to apply between logical and physical pixels in addition
        to the actual scale factor determined by the backend. This option
        allows the scale factor to be adjusted for testing.
    bgcolor : Color
        The background color to use.

    See also
    --------
    vispy.app.Canvas

    Notes
    -----
    Receives the following events:

        * initialize
        * resize
        * draw
        * mouse_press
        * mouse_release
        * mouse_double_click
        * mouse_move
        * mouse_wheel
        * key_press
        * key_release
        * stylus
        * touch
        * close

    The ordering of the mouse_double_click, mouse_press, and mouse_release
    events are not guaranteed to be consistent between backends. Only certain
    backends natively support double-clicking (currently Qt and WX); on other
    backends, they are detected manually with a fixed time delay.
    This can cause problems with accessibility, as increasing the OS detection
    time or using a dedicated double-click button will not be respected.
    """
    def __init__(self, title='Vispy canvas', size=(800, 600), position=None,
                 show=False, autoswap=True, app=None, create_native=True,
                 vsync=False, resizable=True, decorate=True, fullscreen=False,
                 config=None, shared=None, keys=None, parent=None, dpi=None,
                 always_on_top=False, px_scale=1, bgcolor='black'):
        self._scene = None
        # A default widget that follows the shape of the canvas
        self._central_widget = None
        self._draw_order = weakref.WeakKeyDictionary()
        self._fb_stack = []
        self._vp_stack = []
        self._mouse_handler = None
        self.transforms = TransformSystem(canvas=self)
        self._bgcolor = Color(bgcolor).rgba

        super(SceneCanvas, self).__init__(
            title, size, position, show, autoswap, app, create_native, vsync,
            resizable, decorate, fullscreen, config, shared, keys, parent, dpi,
            always_on_top, px_scale)
        self.events.mouse_press.connect(self._process_mouse_event)
        self.events.mouse_move.connect(self._process_mouse_event)
        self.events.mouse_release.connect(self._process_mouse_event)
        self.events.mouse_wheel.connect(self._process_mouse_event)

        self.scene = SubScene()
        self.scene._set_canvas(self)
        self.scene.events.children_change.connect(self._update_scenegraph)
        
    @property
    def scene(self):
        """ The SubScene object that represents the root node of the
        scene graph to be displayed.
        """
        return self._scene

    @scene.setter
    def scene(self, e):
        if self._scene is not None:
            self._scene.events.update.disconnect(self._scene_update)
        self._scene = e
        self._scene.events.update.connect(self._scene_update)

    @property
    def central_widget(self):
        """ Returns the default widget that occupies the entire area of the
        canvas. 
        """
        if self._central_widget is None:
            self._central_widget = Widget(size=self.size, parent=self.scene)
        return self._central_widget

    def _scene_update(self, event):
        self.update()

    @property
    def bgcolor(self):
        return Color(self._bgcolor)

    @bgcolor.setter
    def bgcolor(self, color):
        self._bgcolor = Color(color).rgba
        if hasattr(self, '_backend'):
            self.update()

    def on_draw(self, event):
        """Draw handler

        Parameters
        ----------
        event : instance of Event
            The draw event.
        """
        if self._scene is None:
            return  # Can happen on initialization
        logger.debug('Canvas draw')

        self._draw_scene()

    def render(self, region=None, size=None, bgcolor=None):
        """ Render the scene to an offscreen buffer and return the image array.
        
        Parameters
        ----------
        region : tuple | None
            Specifies the region of the canvas to render. Format is 
            (x, y, w, h). By default, the entire canvas is rendered.
        size : tuple | None
            Specifies the size of the image array to return. If no size is 
            given, then the size of the *region* is used. This argument allows
            the scene to be rendered at resolutions different from the native
            canvas resolution.

        Returns
        -------
        image : array
            Numpy array of type ubyte and shape (h, w, 4). Index [0, 0] is the 
            upper-left corner of the rendered region.
        
        """
        self.set_current()
        # Set up a framebuffer to render to
        offset = (0, 0) if region is None else region[:2]
        csize = self.size if region is None else region[2:]
        size = csize if size is None else size
        fbo = gloo.FrameBuffer(color=gloo.RenderBuffer(size[::-1]),
                               depth=gloo.RenderBuffer(size[::-1]))

        self.push_fbo(fbo, offset, csize)
        try:
            self._draw_scene(bgcolor=bgcolor)
            return fbo.read()
        finally:
            self.pop_fbo()

    def _draw_scene(self, bgcolor=None):
        if bgcolor is None:
            bgcolor = self._bgcolor
        self.context.clear(color=bgcolor, depth=True)
        # Draw the scene, but first disconnect its change signal--
        # any changes that take place during the paint should not trigger
        # a subsequent repaint.
        with self.scene.events.update.blocker(self._scene_update):
            self.draw_visual(self.scene)

    def draw_visual(self, visual, event=None):
        """ Draw a visual and its children to the canvas or currently active
        framebuffer.
        
        Parameters
        ----------
        visual : Visual
            The visual to draw
        event : None or DrawEvent
            Optionally specifies the original canvas draw event that initiated
            this draw.
        """
        # make sure this canvas's context is active
        self.set_current()
        
        # get order to draw visuals
        if visual not in self._draw_order:
            self._draw_order[visual] = self._generate_draw_order()
        order = self._draw_order[visual]
        tr = order[2][0].transforms.get_transform()
        
        # draw (while avoiding branches with visible=False)
        stack = []
        invisible_node = None
        for node, start in order:
            if start:
                stack.append(node)
                if invisible_node is None:
                    if not node.visible:
                        # disable drawing until we exit this node's subtree
                        invisible_node = node
                    else:
                        if hasattr(node, 'draw'):
                            node.draw()
            else:
                if node is invisible_node:
                    invisible_node = None
                stack.pop()

    def _generate_draw_order(self, node=None):
        """Return a list giving the order to draw visuals.
        
        Each node appears twice in the list--(node, True) appears before the
        node's children are drawn, and (node, False) appears after.
        """
        if node is None:
            node = self._scene
        order = [(node, True)]
        for ch in node.children:
            order.extend(self._generate_draw_order(ch))
        order.append((node, False))
        return order

    def _update_scenegraph(self, event):
        """Called when topology of scenegraph has changed.
        """
        self._draw_order.clear()
        self.update()

    def _process_mouse_event(self, event):
        prof = Profiler()
        picked = self.visual_at(event.pos)
        print picked
        
        if self._mouse_handler is None:
            if event.type == 'mouse_press':
                picked = self.visual_at(event.pos)
                self._mouse_handler = picked
            else:
                picked = None
        else:
            picked = self._mouse_handler
            if event.type == 'mouse_release':
                self._mouse_handler = None
        
        if picked is None:
            return
        
        scene_event = SceneMouseEvent(event=event, visual=picked)
        #getattr(picked.events, event.type)(scene_event)
        self.update()
        # If something in the scene handled the scene_event, then we mark
        # the original event accordingly.
        event.handled = scene_event.handled

    def visual_at(self, pos):
        """Return the visual at *pos*.
        """
        tr = self.transforms.get_transform('canvas', 'framebuffer')
        pos = tr.map(pos)[:2]

        try:
            self._scene.picking = True
            img = self.render(tuple(pos) + (1, 1), bgcolor=(0, 0, 0, 0))
        finally:
            self._scene.picking = False
        id = struct.unpack('<I', struct.pack('<4B', *tuple(img[0, 0])))[0]
        print id, img
        return VisualNode._visual_ids.get(id, None)

    def on_resize(self, event):
        """Resize handler

        Parameters
        ----------
        event : instance of Event
            The resize event.
        """
        self._update_transforms()
        
        if self._central_widget is not None:
            self._central_widget.size = self.size
            
        if len(self._vp_stack) == 0:
            self.context.set_viewport(0, 0, *self.physical_size)

    # -------------------------------------------------- transform handling ---
    def push_viewport(self, viewport):
        """ Push a viewport (x, y, w, h) on the stack. Values must be integers
        relative to the active framebuffer.

        Parameters
        ----------
        viewport : tuple
            The viewport as (x, y, w, h).
        """
        vp = list(viewport)
        # Normalize viewport before setting;
        if vp[2] < 0:
            vp[0] += vp[2]
            vp[2] *= -1
        if vp[3] < 0:
            vp[1] += vp[3]
            vp[3] *= -1

        self._vp_stack.append(vp)
        try:
            self.context.set_viewport(*vp)
        except:
            self._vp_stack.pop()
            raise
        
        self._update_transforms()

    def pop_viewport(self):
        """ Pop a viewport from the stack.
        """
        vp = self._vp_stack.pop()
        # Activate latest
        if len(self._vp_stack) > 0:
            self.context.set_viewport(*self._vp_stack[-1])
        else:
            self.context.set_viewport(0, 0, *self.physical_size)
        
        self._update_transforms()
        return vp

    def push_fbo(self, fbo, offset, csize):
        """ Push an FBO on the stack, together with the new viewport.
        and the transform to the FBO.

        Parameters
        ----------
        fbo : instance of FrameBuffer
            The framebuffer.
        offset : tuple
            The offset.
        csize : tuple
            The size to use.
        """
        self._fb_stack.append((fbo, offset, csize))
        try:
            fbo.activate()
            h, w = fbo.color_buffer.shape[:2]
            self.push_viewport((0, 0, w, h))
        except Exception:
            self._fb_stack.pop()
            raise
        
        self._update_transforms()

    def pop_fbo(self):
        """ Pop an FBO from the stack.
        """
        fbo = self._fb_stack.pop()
        fbo[0].deactivate()
        self.pop_viewport()
        if len(self._fb_stack) > 0:
            old_fbo = self._fb_stack[-1]
            old_fbo[0].activate()
        
        self._update_transforms()
        return fbo
        
    def _current_framebuffer(self):
        """ Return (fbo, origin, canvas_size) for the current
        FBO on the stack, or for the canvas if there is no FBO.
        """
        if len(self._fb_stack) == 0:
            return None, (0, 0), self.size
        else:
            return self._fb_stack[-1]

    def _update_transforms(self):
        """Update the canvas's TransformSystem to correct for the current 
        canvas size, framebuffer, and viewport.
        """
        if len(self._fb_stack) == 0:
            fb_size = fb_rect = None
        else:
            fb, origin, fb_size = self._fb_stack[-1]
            fb_rect = origin + fb_size
            
        if len(self._vp_stack) == 0:
            viewport = None
        else:
            viewport = self._vp_stack[-1]
        
        self.transforms.auto_configure(viewport=viewport, fbo_size=fb_size,
                                       fbo_rect=fb_rect)
