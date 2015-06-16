# -*- coding: utf-8 -*-
# Copyright (c) 2015, Vispy Development Team.
# Distributed under the (new) BSD License. See LICENSE.txt for more info.
"""

Definitions
===========

Visual : an object that (1) can be drawn on-screen, (2) can be manipulated
by configuring the coordinate transformations that it uses.

View : a special type of visual that (1) draws the contents of another visual,
(2) using a different set of transforms. Views have only the basic visual
interface (draw, bounds, attach, etc.) and lack access to the specific features
of the visual they are linked to (for example, LineVisual has a ``set_data()``
method, but there is no corresponding method on a view of a LineVisual).


Class Structure
===============

* `BaseVisual` - provides transforms and view creation
  This class lays out the basic API for all visuals: ``draw()``, ``bounds()``,
  ``view()``, and ``attach()`` methods, as well as a `TransformSystem` instance
  that determines where the visual will be drawn.
    * `Visual` - defines a shader program to draw.
      Subclasses are responsible for supplying the shader code and configuring
      program variables, including transforms.
        * `VisualView` - clones the shader program from a Visual instance.
          Instances of `VisualView` contain their own shader program, 
          transforms and filter attachments, and generally behave like a normal
          instance of `Visual`.
    * `CompoundVisual` - wraps multiple Visual instances.
      These visuals provide no program of their own, but instead rely on one or
      more internally generated `Visual` instances to do their drawing. For
      example, a PolygonVisual consists of an internal LineVisual and
      MeshVisual.
        * `CompoundVisualView` - wraps multiple VisualView instances.
          This allows a `CompoundVisual` to be viewed with a different set of
          transforms and filters.


Making Visual Subclasses
========================

When making subclasses of `Visual`, it is only necessary to reimplement the 
``_prepare_draw()``, ``_prepare_transforms()``, and ``_compute_bounds()``
methods. These methods will be called by the visual automatically when it is
needed for itself or for a view of the visual.

It is important to remember
when implementing these methods that most changes made to the visual's shader
program should also be made to the programs for each view. To make this easier,
the visual uses a `MultiProgram`, which allows all shader programs across the 
visual and its views to be accessed simultaneously. For example::

    def _prepare_draw(self, view):
        # This line applies to the visual and all of its views
        self.shared_program['a_position'] = self._vbo
        
        # This line applies only to the view that is about to be drawn
        view.view_program['u_color'] = (1, 1, 1, 1)
        
Under most circumstances, it is not necessary to reimplement `VisualView`
because a view will directly access the ``_prepare`` and ``_compute`` methods
from the visual it is viewing. However, if the `Visual` to be viewed is a 
subclass that reimplements other methods such as ``draw()`` or ``bounds()``,
then it will be necessary to provide a new matching `VisualView` subclass. 


Making CompoundVisual Subclasses
================================

Compound visual subclasses are generally very easy to construct::

    class PlotLineVisual(visuals.CompoundVisual):
        def __init__(self, ...):
            self._line = LineVisual(...)
            self._point = PointVisual(...)
            visuals.CompoundVisual.__init__(self, [self._line, self._point])

A compound visual will automatically handle forwarding transform system changes
and filter attachments to its internally-wrapped visuals. To the user, this
will appear to behave as a single visual.
"""

from __future__ import division
import weakref

from ..util.event import EmitterGroup, Event
from .shaders import StatementList, MultiProgram
from .transforms import TransformSystem
from .. import gloo


class VisualShare(object):
    """Contains data that is shared between all views of a visual.
    
    This includes:
    
    * GL state variables (blending, depth test, etc.)
    * A weak dictionary of all views
    * A list of filters that should be applied to all views
    * A cache for bounds.
    """
    def __init__(self):
        # Note: in some cases we will need to compute bounds independently for each
        # view. That will have to be worked out later..
        self.bounds = {}
        self.gl_state = {}
        self.views = weakref.WeakKeyDictionary()
        self.filters = []


class BaseVisual(object):
    """Superclass for all visuals.
    
    This class provides:
    
    * A TransformSystem.
    * Two events: `update` and `bounds_change`.
    * Minimal framework for creating views of the visual.
    * A data structure that is shared between all views of the visual.
    * Abstract `draw`, `bounds`, `attach`, and `detach` methods.
    
    Notes
    -----
    When used in the scenegraph, all Visual classes are mixed with
    `vispy.scene.Node` in order to implement the methods, attributes and
    capabilities required for their usage within it.
    """
    def __init__(self, vshare=None):
        self._view_class = getattr(self, '_view_class', VisualView)
        
        if vshare is None:
            vshare = VisualShare()
        
        self._vshare = vshare
        self._vshare.views[self] = None
        
        self.events = EmitterGroup(source=self,
                                   auto_connect=True,
                                   update=Event,
                                   bounds_change=Event
                                   )
        
        self.transforms = TransformSystem()
        self.transforms.changed.connect(self._transform_changed)

    @property
    def transform(self):
        return self.transforms.visual_transform.transforms[0]
    
    @transform.setter
    def transform(self, tr):
        self.transforms.visual_transform = tr

    def get_transform(self, map_from='visual', map_to='render'):
        return self.transforms.get_transform(map_from, map_to)

    def view(self):
        """Return a new view of this visual.
        """
        return self._view_class(self)

    def draw(self):
        raise NotImplementedError()

    def bounds(self, axis):
        raise NotImplementedError()
        
    def attach(self, filter):
        raise NotImplementedError()
        
    def detach(self, filter):
        raise NotImplementedError()

    def update(self):
        self.events.update()

    def _transform_changed(self, event):
        self.update()


class BaseVisualView(object):
    """Base class for a view on a visual.
    
    This class must be mixed with another Visual class to work properly. It 
    works mainly by forwarding the calls to _prepare_draw, _prepare_transforms,
    and _compute_bounds to the viewed visual.
    """
    def __init__(self, visual):
        self._visual = visual
        
    @property
    def visual(self):
        return self._visual
        
    def _prepare_draw(self, view=None):
        self._visual._prepare_draw(view=view)
        
    def _prepare_transforms(self, view):
        self._visual._prepare_transforms(view)
    
    def _compute_bounds(self, axis, view):
        self._visual._compute_bounds(axis, view)
        
    def __repr__(self):
        return '<%s on %r>' % (self.__class__.__name__, self._visual)


class Visual(BaseVisual):
    """Base class for all visuals that can be drawn using a single shader 
    program.
    
    This class creates a MultiProgram, which is an object that 
    behaves like a normal shader program (you can assign shader code, upload
    values, set template variables, etc.) but internally manages multiple 
    ModularProgram instances, one per view.
    
    Subclasses generally only need to reimplement _compute_bounds,
    _prepare_draw, and _prepare_transforms.
    """
    def __init__(self, vcode='', fcode='', program=None, _vshare=None):
        self._view_class = VisualView
        BaseVisual.__init__(self, _vshare)
        if _vshare is None:
            self._vshare.draw_mode = 'triangles'
            self._vshare.index_buffer = None
            if program is None:
                self._vshare.program = MultiProgram(vcode, fcode)
            else:
                self._vshare.program = program
                if len(vcode) > 0 or len(fcode) > 0:
                    raise ValueError("Cannot specify both program and "
                        "vcode/fcode arguments.")
        
        self._program = self._vshare.program.add_program()
        self._prepare_transforms(self)
        self._filters = []
        self._hooks = {}

    def set_gl_state(self, preset=None, **kwargs):
        """Define the set of GL state parameters to use when drawing

        Parameters
        ----------
        preset : str
            Preset to use.
        **kwargs : dict
            Keyword argments to use.
        """
        self._vshare.gl_state = kwargs
        self._vshare.gl_state['preset'] = preset
    
    def update_gl_state(self, *args, **kwargs):
        """Modify the set of GL state parameters to use when drawing

        Parameters
        ----------
        *args : tuple
            Arguments.
        **kwargs : dict
            Keyword argments.
        """
        if len(args) == 1:
            self._vshare.gl_state['preset'] = args[0]
        elif len(args) != 0:
            raise TypeError("Only one positional argument allowed.")
        self._vshare.gl_state.update(kwargs)

    def bounds(self, axis):
        cache = self.vshare.bounds
        if axis not in cache:
            cache[axis] = self._compute_bounds(axis, view=self)
        return cache[axis]

    def _compute_bounds(self, axis, view):
        """Return the (min, max) bounding values of this visual along *axis*
        in the local coordinate system.
        """
        raise NotImplementedError()

    def _prepare_draw(self, view=None):
        """This visual is about to be drawn.
        
        Visuals must implement this method to ensure that all program 
        and GL state variables are updated immediately before drawing.
        
        Return False to indicate that the visual should not be drawn.
        """
        raise NotImplementedError()

    def _prepare_transforms(self, view):
        """Assign a view's transforms to the proper shader template variables
        on the view's shader program. 
        """
        
        # Todo: this method can be removed if we somehow enable the shader
        # to specify exactly which transform functions it needs by name. For
        # example:
        #
        #     // mapping function is automatically defined from the 
        #     // corresponding transform in the view's TransformSystem
        #     gl_Position = visual_to_render(a_position);
        #     
        raise NotImplementedError()

    @property
    def shared_program(self):
        return self._vshare.program

    @property
    def view_program(self):
        return self._program

    @property
    def _draw_mode(self):
        return self._vshare.draw_mode
    
    @_draw_mode.setter
    def _draw_mode(self, m):
        self._vshare.draw_mode = m
        
    @property
    def _index_buffer(self):
        return self._vshare.index_buffer
        
    @_index_buffer.setter
    def _index_buffer(self, buf):
        self._vshare.index_buffer = buf
        
    def draw(self):
        gloo.set_state(**self._vshare.gl_state)
        if self._prepare_draw(view=self) is False:
            return
        self._program.draw(self._vshare.draw_mode, self._vshare.index_buffer)
        
    def bounds(self):
        # check self._vshare for cached bounds before computing
        return None
        
    def _get_hook(self, shader, name):
        """Return a FunctionChain that Filters may use to modify the program.

        *shader* should be "frag" or "vert"
        *name* should be "pre" or "post"
        """
        assert name in ('pre', 'post')
        key = (shader, name)
        if key in self._hooks:
            return self._hooks[key]
        hook = StatementList()
        if shader == 'vert':
            self.view_program.vert[name] = hook
        elif shader == 'frag':
            self.view_program.frag[name] = hook
        self._hooks[key] = hook
        return hook
        
    def attach(self, filter, view=None):
        """Attach a Filter to this visual. 
        
        Each filter modifies the appearance or behavior of the visual.

        Parameters
        ----------
        filt : object
            The filter to attach.
        """
        if view is None:
            self._vshare.filters.append(filter)
            for view in self._vshare.views.keys():
                filter._attach(view)
        else:
            view._filters.append(filter)
            filter._attach(view)
        
    def detach(self, filter, view=None):
        """Detach a filter.

        Parameters
        ----------
        filt : object
            The filter to detach.
        """
        if view is None:
            self._vshare.filters.remove(filter)
            for view in self._vshare.views.keys():
                filter._detach(view)
        else:
            view._filters.remove(filter)
            filter._detach(view)
        

class VisualView(BaseVisualView, Visual):
    """A view on another Visual instance.
    
    View instances are created by calling ``visual.view()``.
    
    Because this is a subclass of `Visual`, all instances of `VisualView` 
    define their own shader program (which is a clone of the viewed visual's
    program), transforms, and filter attachments. 
    """
    def __init__(self, visual):
        BaseVisualView.__init__(self, visual)
        Visual.__init__(self, _vshare=visual._vshare)
        
        # Attach any shared filters 
        for filter in self._vshare.filters:
            filter._attach(self)

        
class CompoundVisual(BaseVisual):
    """Visual consisting entirely of sub-visuals.

    To the user, a compound visual behaves exactly like a normal visual--it
    has a transform system, draw() and bounds() methods, etc. Internally, the
    compound visual automatically manages proxying these transforms and methods
    to its sub-visuals.
    
    Parameters
    ----------
    
    subvisuals : list of BaseVisual instances
        The list of visuals to be combined in this compound visual.
    
    Notes
    -----
    
    Sub-visuals may optionally be given a boolean ``visible`` attribute that
    can be used to hide or show each.
    """
    def __init__(self, subvisuals):
        self._view_class = CompoundVisualView
        BaseVisual.__init__(self)
        self._subvisuals = []
        for v in subvisuals:
            self.add_subvisual(v)
        
    def add_subvisual(self, visual):
        visual.transforms = self.transforms
        visual._prepare_transforms(visual)
        if not hasattr(visual, 'visible'):
            visual.visible = True
        self._subvisuals.append(visual)
        self.update()

    def remove_subvisual(self, visual):
        self._subvisuals.remove(visuals)
        self.update()
        
    def draw(self):
        if self._prepare_draw(view=self) is False:
            return
        for v in self._subvisuals:
            if v.visible:
                v.draw()

    def _prepare_draw(self, view):
        pass

    def _prepare_transforms(self, view):
        for v in view._subvisuals:
            v._prepare_transforms(v)
            
    def set_gl_state(self, preset=None, **kwargs):
        for v in self._subvisuals:
            v.set_gl_state(preset=preset, **kwargs)
    
    def update_gl_state(self, *args, **kwargs):
        for v in self._subvisuals:
            v.update_gl_state(*args, **kwargs)

    def attach(self, filter, view=None):
        for v in self._subvisuals:
            v.attach(filter, v)
    
    def detach(self, filter, view=None):
        for v in self._subvisuals:
            v.detach(filter, v)
    
    def _compute_bounds(self, axis, view):
        bounds = None
        for v in self._subvisuals:
            if v.visible:
                vb = b.bounds(axis, view)
                if bounds is None:
                    bounds = vb
                else:
                    bounds = [min(bounds[0], vb[0]), max(bounds[1], vb[1])]
        return bounds
    

class CompoundVisualView(BaseVisualView, CompoundVisual):
    def __init__(self, visual):
        BaseVisualView.__init__(self, visual)
        # Create a view on each sub-visual 
        subv = [v.view() for v in visual._subvisuals]
        CompoundVisual.__init__(self, subv)

        # Attach any shared filters 
        for filter in self._vshare.filters:
            for v in self._subvisuals:
                filter._attach(v)        
