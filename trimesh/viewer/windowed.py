"""
windowed.py
---------------

Provides a pyglet- based windowed viewer to preview
Trimesh, Scene, PointCloud, and Path objects.

Works on all major platforms: Windows, Linux, and OSX.
"""

import pyglet
pyglet.options['shadow_window'] = False
import pyglet.gl as gl

import numpy as np

import platform
import collections

from .. import rendering
from ..transformations import Arcball
from ..util import log, BytesIO
from ..visual import to_rgba

# smooth only when fewer faces than this
_SMOOTH_MAX_FACES = 100000


class SceneViewer(pyglet.window.Window):

    def __init__(self,
                 scene,
                 smooth=True,
                 flags=None,
                 visible=True,
                 resolution=None,
                 start_loop=True,
                 callback=None,
                 callback_period=None,
                 caption=None,
                 fixed=None,
                 **kwargs):
        """
        Create a window that will display a trimesh.Scene object
        in an OpenGL context via pyglet.

        Parameters
        ---------------
        scene : trimesh.scene.Scene
          Scene with geometry and transforms
        smooth : bool
          If True try to smooth shade things
        flags : dict
          If passed apply keys to self.view:
          ['cull', 'wireframe', etc]
        visible : bool
          Display window or not
        resolution : (2,) int
          Initial resolution of window
        start_loop : bool
          Call pyglet.app.run() at the end of init
        callback : function
          A function which can be called periodically to
          update things in the scene
        callback_period : float
          How often to call the callback, in seconds
        fixed : None or iterable
          List of keys in scene.geometry to skip view
          transform on to keep fixed relative to camera
        kwargs : dict
          Additional arguments to pass, including
          'background' for to set background color
        """
        self.scene = self._scene = scene
        self.callback = callback
        self.callback_period = callback_period
        self.scene._redraw = self._redraw
        self.reset_view(flags=flags)
        self.batch = pyglet.graphics.Batch()

        # store kwargs
        self.kwargs = kwargs

        # store a vertexlist for an axis marker
        self._axis = None
        # store scene geometry as vertex lists
        self.vertex_list = {}
        # store geometry hashes
        self.vertex_list_hash = {}
        # store geometry rendering mode
        self.vertex_list_mode = {}
        # store meshes that don't rotate relative to viewer
        self.fixed = fixed
        # name : texture
        self.textures = {}

        # if resolution isn't defined set a default value
        if resolution is None:
            resolution = scene.camera.resolution
        else:
            scene.camera.resolution = resolution

        try:
            # try enabling antialiasing
            # if you have a graphics card this will probably work
            conf = gl.Config(sample_buffers=1,
                             samples=4,
                             depth_size=24,
                             double_buffer=True)
            super(SceneViewer, self).__init__(config=conf,
                                              visible=visible,
                                              resizable=True,
                                              width=resolution[0],
                                              height=resolution[1],
                                              caption=caption)
        except pyglet.window.NoSuchConfigException:
            conf = gl.Config(double_buffer=True)
            super(SceneViewer, self).__init__(config=conf,
                                              resizable=True,
                                              visible=visible,
                                              width=resolution[0],
                                              height=resolution[1],
                                              caption=caption)

        # add scene geometry to viewer geometry
        for name, mesh in scene.geometry.items():
            self.add_geometry(name=name,
                              geometry=mesh,
                              smooth=bool(smooth))

        # call after geometry is added
        self.init_gl()
        self.set_size(*resolution)
        self.update_flags()

        # someone has passed a callback to be called periodically
        if self.callback is not None:
            # if no callback period is specified set it to default
            if callback_period is None:
                callback_period = 1.0 / 100.0
            # set up a do-nothing periodic task which will
            # trigger `self.on_draw` every `callback_period`
            # seconds if someone has passed a callback
            pyglet.clock.schedule_interval(lambda x: x,
                                           callback_period)
        if start_loop:
            pyglet.app.run()

    def _redraw(self):
        self.on_draw()

    def _update_meshes(self):
        # call the callback if specified
        if self.callback is not None:
            self.callback(self.scene)

    def add_geometry(self, name, geometry, **kwargs):
        """
        Add a geometry to the viewer.

        Parameters
        --------------
        name : hashable
          Name that references geometry
        geometry : Trimesh, Path2D, Path3D, PointCloud
          Geometry to display in the viewer window
        kwargs **
          Passed to rendering.convert_to_vertexlist
        """
        # convert geometry to constructor args
        args = rendering.convert_to_vertexlist(geometry, **kwargs)
        # create the indexed vertex list
        self.vertex_list[name] = self.batch.add_indexed(*args)
        # save the MD5 of the geometry
        self.vertex_list_hash[name] = geometry_hash(geometry)
        # save the rendering mode from the constructor args
        self.vertex_list_mode[name] = args[1]

        # if a geometry has a texture defined convert it to opengl form and
        # save
        if hasattr(geometry, 'visual') and hasattr(
                geometry.visual, 'material'):
            tex = rendering.material_to_texture(geometry.visual.material)
            if tex is not None:
                self.textures[name] = tex

    def reset_view(self, flags=None):
        """
        Set view to the default view.

        Parameters
        --------------
        flags : None or dict
          If any view key passed override the default
          e.g. {'cull': False}
        """
        self.view = {'cull': True,
                     'axis': False,
                     'fullscreen': False,
                     'wireframe': False,
                     'translation': np.zeros(3),
                     'center': self.scene.centroid,
                     'scale': self.scene.scale,
                     'ball': Arcball()}

        try:
            # place the arcball (rotation widget) in the center of the view
            self.view['ball'].place([self.width / 2.0,
                                     self.height / 2.0],
                                    (self.width + self.height) / 2.0)

            # if any flags are passed override defaults
            if isinstance(flags, dict):
                for k, v in flags.items():
                    if k in self.view:
                        self.view[k] = v
                self.update_flags()
        except BaseException:
            pass

    def init_gl(self):
        """
        Perform the magic incantations to create an
        OpenGL scene using pyglet.
        """

        # default background color is white-ish
        background = [.99, .99, .99, 1.0]
        # if user passed a background color use it
        if 'background' in self.kwargs:
            try:
                # convert to (4,) uint8 RGBA
                background = to_rgba(self.kwargs['background'])
                # convert to 0.0 - 1.0 float
                background = background.astype(np.float64) / 255.0
            except BaseException:
                log.error('background color set but wrong!',
                          exc_info=True)

        # apply the background color
        gl.glClearColor(*background)

        # find the maximum depth based on
        # maximum length of scene AABB
        max_depth = (np.abs(self.scene.bounds).max(axis=1) ** 2).sum() ** .5
        max_depth = np.clip(max_depth, 500.00, np.inf)
        gl.glDepthRange(0.0, max_depth)

        gl.glClearDepth(1.0)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LEQUAL)

        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_CULL_FACE)

        # do some openGL things
        gl.glColorMaterial(gl.GL_FRONT_AND_BACK,
                           gl.GL_AMBIENT_AND_DIFFUSE)
        gl.glEnable(gl.GL_COLOR_MATERIAL)
        gl.glShadeModel(gl.GL_SMOOTH)

        gl.glMaterialfv(gl.GL_FRONT,
                        gl.GL_AMBIENT,
                        rendering.vector_to_gl(
                            0.192250, 0.192250, 0.192250))
        gl.glMaterialfv(gl.GL_FRONT,
                        gl.GL_DIFFUSE,
                        rendering.vector_to_gl(
                            0.507540, 0.507540, 0.507540))
        gl.glMaterialfv(gl.GL_FRONT,
                        gl.GL_SPECULAR,
                        rendering.vector_to_gl(
                            .5082730, .5082730, .5082730))

        gl.glMaterialf(gl.GL_FRONT,
                       gl.GL_SHININESS,
                       .4 * 128.0)

        # enable blending for transparency
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA,
                       gl.GL_ONE_MINUS_SRC_ALPHA)

        # make the lines from Path3D objects less ugly
        gl.glEnable(gl.GL_LINE_SMOOTH)
        gl.glHint(gl.GL_LINE_SMOOTH_HINT, gl.GL_NICEST)
        # set the width of lines to 1.5 pixels
        gl.glLineWidth(1.5)
        # set PointCloud markers to 4 pixels in size
        gl.glPointSize(4)

        # set up the viewer lights using self.scene
        self.update_lighting()

    def update_lighting(self):
        """
        Take the lights defined in scene.lights and
        apply them as openGL lights.
        """
        gl.glEnable(gl.GL_LIGHTING)
        # opengl only supports 7 lights?
        for i, light in enumerate(self.scene.lights[:7]):
            # the index of which light we have
            lightN = eval('gl.GL_LIGHT{}'.format(i))

            # get the transform for the light by name
            matrix = self.scene.graph[light.name][0]

            # convert light object to glLightfv calls
            multiargs = rendering.light_to_gl(
                light=light,
                transform=matrix,
                lightN=lightN)

            # enable the light in question
            gl.glEnable(lightN)
            # run the glLightfv calls
            for args in multiargs:
                gl.glLightfv(*args)

    def toggle_culling(self):
        """
        Toggle back face culling.

        It is on by default but if you are dealing with
        non- watertight meshes you probably want to be able
        to see the back sides.
        """
        self.view['cull'] = not self.view['cull']
        self.update_flags()

    def toggle_wireframe(self):
        """
        Toggle wireframe mode

        Good for  looking inside meshes, off by default.
        """
        self.view['wireframe'] = not self.view['wireframe']
        self.update_flags()

    def toggle_fullscreen(self):
        """
        Toggle between fullscreen and windowed mode.
        """
        self.view['fullscreen'] = not self.view['fullscreen']
        self.update_flags()

    def toggle_axis(self):
        """
        Toggle a rendered XYZ/RGB axis marker:
        off, world frame, every frame
        """
        # cycle through three axis states
        states = [False, 'world', 'all']
        # the state after toggling
        index = (states.index(self.view['axis']) + 1) % len(states)
        # update state to next index
        self.view['axis'] = states[index]
        # perform gl actions
        self.update_flags()

    def update_flags(self):
        """
        Check the view flags, and call required GL functions.
        """
        # view mode, filled vs wirefrom
        if self.view['wireframe']:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        else:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        # set fullscreen or windowed
        self.set_fullscreen(fullscreen=self.view['fullscreen'])

        # backface culling on or off
        if self.view['cull']:
            gl.glEnable(gl.GL_CULL_FACE)
        else:
            gl.glDisable(gl.GL_CULL_FACE)

        # case where we WANT an axis and NO vertexlist
        # is stored internally
        if self.view['axis'] and self._axis is None:
            from .. import creation
            # create an axis marker sized relative to the scene
            axis = creation.axis(origin_size=self.scene.scale / 100)
            # create ordered args for a vertex list
            args = rendering.mesh_to_vertexlist(axis)
            # store the axis as a reference
            self._axis = self.batch.add_indexed(*args)

        # case where we DON'T want an axis but a vertexlist
        # IS stored internally
        elif not self.view['axis'] and self._axis is not None:
            # remove the axis from the rendering batch
            self._axis.delete()
            # set the reference to None
            self._axis = None

    def on_resize(self, width, height):
        """
        Handle resized windows.
        """
        try:
            # for high DPI screens viewport size
            # will be different then the passed size
            width, height = self.get_viewport_size()
        except BaseException:
            # older versions of pyglet may not have this
            pass

        # set the new viewport size
        gl.glViewport(0, 0, width, height)
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()

        # get field of view from camera
        fovY = self.scene.camera.fov[1]
        gl.gluPerspective(fovY,
                          width / float(height),
                          .01,
                          self.scene.scale * 5.0)
        gl.glMatrixMode(gl.GL_MODELVIEW)
        self.view['ball'].place([width / 2, height / 2],
                                (width + height) / 2)

    def on_mouse_press(self, x, y, buttons, modifiers):
        """
        Set the start point of the drag.
        """
        self.view['ball'].down([x, -y])

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        """
        Pan or rotate the view.
        """
        # left mouse button, with control key down (pan)
        if ((buttons == pyglet.window.mouse.LEFT) and
                (modifiers & pyglet.window.key.MOD_CTRL)):
            delta = [dx / self.width, dy / self.height]
            self.view['translation'][:2] += delta

        # left mouse button, no modifier keys pressed (rotate)
        elif (buttons == pyglet.window.mouse.LEFT):
            self.view['ball'].drag([x, -y])

    def on_mouse_scroll(self, x, y, dx, dy):
        """
        Zoom the view.
        """
        self.view['translation'][2] += float(dy) / self.height

    def on_key_press(self, symbol, modifiers):
        """
        Call appropriate functions given key presses.
        """
        magnitude = 10
        if symbol == pyglet.window.key.W:
            self.toggle_wireframe()
        elif symbol == pyglet.window.key.Z:
            self.reset_view()
        elif symbol == pyglet.window.key.C:
            self.toggle_culling()
        elif symbol == pyglet.window.key.A:
            self.toggle_axis()
        elif symbol == pyglet.window.key.Q:
            self.close()
        elif symbol == pyglet.window.key.M:
            self.maximize()
        elif symbol == pyglet.window.key.F:
            self.toggle_fullscreen()
        elif symbol == pyglet.window.key.LEFT:
            self.view['ball'].down([0, 0])
            self.view['ball'].drag([-magnitude, 0])
        elif symbol == pyglet.window.key.RIGHT:
            self.view['ball'].down([0, 0])
            self.view['ball'].drag([magnitude, 0])
        elif symbol == pyglet.window.key.DOWN:
            self.view['ball'].down([0, 0])
            self.view['ball'].drag([0, -magnitude])
        elif symbol == pyglet.window.key.UP:
            self.view['ball'].down([0, 0])
            self.view['ball'].drag([0, magnitude])

    def on_draw(self):
        """
        Run the actual draw calls.
        """
        self._update_meshes()
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glLoadIdentity()

        # pull the new camera transform from the scene
        transform_camera = self.scene.graph.get(
            frame_to='world',
            frame_from=self.scene.camera.name)[0]

        # apply the camera transform to the matrix stack
        gl.glMultMatrixf(rendering.matrix_to_gl(transform_camera))

        # dragging the mouse moves the view
        # but doesn't alter the scene
        view = view_to_transform(self.view)
        # add the view transform to the stack
        gl.glMultMatrixf(rendering.matrix_to_gl(view))

        # we want to render fully opaque objects first,
        # followed by objects which have transparency
        node_names = collections.deque(self.scene.graph.nodes_geometry)
        # how many nodes did we start with
        count_original = len(node_names)
        count = -1

        # if we are rendering an axis marker at the world
        if self._axis:
            # we stored it as a vertex list
            self._axis.draw(mode=gl.GL_TRIANGLES)

        while len(node_names) > 0:
            count += 1
            current_node = node_names.popleft()

            # get the transform from world to geometry and mesh name
            transform, geometry_name = self.scene.graph[current_node]

            # if no geometry at this frame continue without rendering
            if geometry_name is None:
                continue

            # if a geometry is marked as fixed apply the inverse view transform
            if self.fixed is not None and geometry_name in self.fixed:
                transform = np.dot(np.linalg.inv(view), transform)

            # get a reference to the mesh so we can check transparency
            mesh = self.scene.geometry[geometry_name]

            # add a new matrix to the model stack
            gl.glPushMatrix()
            # transform by the nodes transform
            gl.glMultMatrixf(rendering.matrix_to_gl(transform))

            # draw an axis marker for each mesh frame
            if self.view['axis'] == 'all':
                self._axis.draw(mode=gl.GL_TRIANGLES)

            # transparent things must be drawn last
            if (hasattr(mesh, 'visual') and
                hasattr(mesh.visual, 'transparency')
                    and mesh.visual.transparency):
                # put the current item onto the back of the queue
                if count < count_original:
                    # add the node to be drawn last
                    node_names.append(current_node)
                    # pop the matrix stack for now
                    gl.glPopMatrix()
                    # come back to this mesh later
                    continue

            # if we have texture enable the target texture
            texture = None
            if geometry_name in self.textures:
                texture = self.textures[geometry_name]
                gl.glEnable(texture.target)
                gl.glBindTexture(texture.target, texture.id)

            # get the mode of the current geometry
            mode = self.vertex_list_mode[geometry_name]
            # draw the mesh with its transform applied
            self.vertex_list[geometry_name].draw(mode=mode)
            # pop the matrix stack as we drew what we needed to draw
            gl.glPopMatrix()

            # disable texture after using
            if texture is not None:
                gl.glDisable(texture.target)

    def save_image(self, file_obj):
        """
        Save the current color buffer to a file object
        in PNG format.

        Parameters
        -------------
        file_obj: file name, or file- like object
        """
        manager = pyglet.image.get_buffer_manager()
        colorbuffer = manager.get_color_buffer()

        # if passed a string save by name
        if hasattr(file_obj, 'write'):
            colorbuffer.save(file=file_obj)
        else:
            colorbuffer.save(filename=file_obj)


def view_to_transform(view):
    """
    Given a dictionary containing view parameters,
    calculate a transformation matrix.
    """
    transform = view['ball'].matrix()
    transform[:3, 3] = view['center']
    transform[:3, 3] -= np.dot(transform[:3, :3], view['center'])
    transform[:3, 3] += view['translation'] * view['scale'] * 5.0
    return transform


def geometry_hash(geometry):
    """
    Get an MD5 for a geometry object

    Parameters
    ------------
    geometry : object

    Returns
    ------------
    MD5 : str
    """
    if hasattr(geometry, 'md5'):
        # for most of our trimesh objects
        md5 = geometry.md5()
    elif hasattr(geometry, 'tostring'):
        # for unwrapped ndarray objects
        md5 = str(hash(geometry.tostring()))

    if hasattr(geometry, 'visual'):
        # if visual properties are defined
        md5 += str(geometry.visual.crc())
    return md5


def render_scene(scene,
                 resolution=(1080, 1080),
                 visible=True,
                 **kwargs):
    """
    Render a preview of a scene to a PNG.

    Parameters
    ------------
    scene : trimesh.Scene
      Geometry to be rendered
    resolution : (2,) int
      Resolution in pixels
    kwargs : **
      Passed to SceneViewer

    Returns
    ---------
    render : bytes
      Image in PNG format
    """
    window = SceneViewer(scene,
                         start_loop=False,
                         visible=visible,
                         resolution=resolution,
                         **kwargs)

    if visible is None:
        visible = platform.system() != 'Linux'

    # need to run loop twice to display anything
    for i in range(2):
        pyglet.clock.tick()
        window.switch_to()
        window.dispatch_events()
        window.dispatch_event('on_draw')
        window.flip()

    # save the color buffer data to memory
    file_obj = BytesIO()
    window.save_image(file_obj)
    file_obj.seek(0)
    render = file_obj.read()
    window.close()

    return render
