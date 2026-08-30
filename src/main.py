import sys
import os
import json
import zipfile
import threading
import re
import ctypes
import ctypes.wintypes
import concurrent.futures
import base64
from io import BytesIO
from collections import OrderedDict

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QDialog, QHBoxLayout, QComboBox, QCheckBox, QPushButton,
                            QColorDialog, QGroupBox, QFormLayout, QSpinBox,
                            QListWidget, QListWidgetItem, QMessageBox,
                            QListView, QSlider)
from PyQt5.QtCore import Qt, QTimer, QObject, QByteArray, QSize, QThread, pyqtSignal, QPoint, QEvent, QBuffer, QIODevice
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QWheelEvent, QTransform, QImageReader,
                        QMovie, QKeyEvent, QCloseEvent, QMouseEvent, QIcon, QColor, QPainter,
                        QPen, QPolygon,
                        QOpenGLContext, QOffscreenSurface, QOpenGLFramebufferObject,
                        QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QVector2D)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PIL_Image = None
PIL_ImageEnhance = None

def get_pil_image():
    global PIL_Image
    if PIL_Image is None:
        from PIL import Image
        PIL_Image = Image
    return PIL_Image

def get_pil_enhance():
    global PIL_ImageEnhance
    if PIL_ImageEnhance is None:
        from PIL import ImageEnhance
        PIL_ImageEnhance = ImageEnhance
    return PIL_ImageEnhance

_cv2_module = None
_np_module = None

def get_cv2():
    """Lazy-loaded like get_pil_image() above. cv2 (opencv-python-headless)
    is an optional dependency used only for the fast animated-frame color
    path (see apply_color_adjustments_cv2) -- if it isn't installed, that
    path's caller falls back to the plain PIL pipeline, so importing it
    lazily here means a missing cv2 install never breaks startup or any
    other feature, only forfeits the speedup."""
    global _cv2_module
    if _cv2_module is None:
        import cv2
        _cv2_module = cv2
    return _cv2_module

def get_numpy():
    global _np_module
    if _np_module is None:
        import numpy
        _np_module = numpy
    return _np_module

# Which algorithm handles saturation. Both are kept side by side so the two
# can be A/B compared directly -- flip this to 'enhance' to go back to the
# original behavior.
#   'matrix'  (default) -- does the exact same blend math as
#             ImageEnhance.Color (out = gray*(1-s) + channel*s, using
#             Pillow's own ITU-R 601-2 luma weights) but as a single 3x4
#             color-matrix convert() instead of building a full-size
#             grayscale "degenerate" image and blending against it.
#             Benchmarked ~20-35% faster across 480p-4K on this machine,
#             with pixel output within +/-1 of 'enhance' (rounding only).
#   'enhance' -- the original ImageEnhance.Color(img).enhance(...) path.
SATURATION_METHOD = 'matrix'

def _saturate_enhance(img, saturation):
    return get_pil_enhance().Color(img).enhance(saturation / 100.0)

def _saturate_matrix(img, saturation):
    s = saturation / 100.0
    lr, lg, lb = 0.299, 0.587, 0.114  # same weights Pillow's convert('L') uses
    if img.mode != 'RGB':
        img = img.convert('RGB')
    matrix = (
        lr * (1 - s) + s, lg * (1 - s),     lb * (1 - s),     0,
        lr * (1 - s),     lg * (1 - s) + s, lb * (1 - s),     0,
        lr * (1 - s),     lg * (1 - s),     lb * (1 - s) + s, 0,
    )
    return img.convert('RGB', matrix)

def apply_color_adjustments(img, saturation=100, brightness=100, contrast=100):
    """Apply saturation/brightness/contrast to a PIL RGB(A) image.

    Visually matches chaining ImageEnhance.Color -> Brightness -> Contrast
    (within +/-1-3 out of 255 from rounding, verified against many slider
    combinations including the 0/200 extremes), but brightness and contrast
    are each a plain per-channel function of the pixel value, so they're
    applied as one fast 256-entry point() lookup table instead of
    ImageEnhance's blend-against-a-full-size-degenerate-image, which
    benchmarked ~2.7x faster for those two alone on a 24MP image. Saturation
    is handled by _saturate_matrix/_saturate_enhance above depending on
    SATURATION_METHOD -- see that constant for how the two compare (a
    hand-written numpy version was also tried for this and was slower than
    either of PIL's own C implementations, so it isn't offered as a third
    option).
    The contrast LUT's pivot is computed from the *current* image (after
    saturation/brightness were already applied, same as ImageEnhance does
    internally) via PIL's own fast ImageStat, so the sequential-clipping
    behavior matches too.
    """
    if saturation != 100:
        if SATURATION_METHOD == 'matrix':
            img = _saturate_matrix(img, saturation)
        else:
            img = _saturate_enhance(img, saturation)
    if brightness != 100:
        b = brightness / 100.0
        lut = [max(0, min(255, round(x * b))) for x in range(256)]
        img = img.point(lut * len(img.getbands()))
    if contrast != 100:
        from PIL import ImageStat
        mean = round(ImageStat.Stat(img.convert('L')).mean[0])
        c = contrast / 100.0
        lut = [max(0, min(255, round(mean + (x - mean) * c))) for x in range(256)]
        img = img.point(lut * len(img.getbands()))
    return img

# ITU-R 601-2 luma weights -- same numbers _saturate_matrix and
# apply_color_adjustments above already use (also what Pillow's own
# convert('L') uses), kept as one named constant so the cv2 path below
# can't drift from them.
_LUMA_R, _LUMA_G, _LUMA_B = 0.299, 0.587, 0.114

def apply_color_adjustments_cv2(rgb, saturation=100, brightness=100, contrast=100):
    """OpenCV/numpy equivalent of apply_color_adjustments() above, for the
    animated gif/webp playback hot path (see _process_animated_frame_fast
    and _submit_animated_frame_processing). Takes and returns an HxWx3
    uint8 numpy array (R,G,B order -- deliberately never converted to
    OpenCV's usual BGR, so this can reuse the exact same weights and
    matrix layout as _saturate_matrix and the contrast math above
    unchanged, instead of re-deriving them for BGR order and risking a
    mismatch between how a static image and an animated one render the
    same slider values).

    Measured end to end (real color math + the RGBA<->RGB buffer
    conversions around it, matching what _submit_animated_frame_processing
    actually does) at roughly 1.3-1.6x apply_color_adjustments()'s speed on
    a single CPU core -- most of the per-frame cost turned out to be
    memory movement (format conversions, buffer copies) rather than the
    saturation/brightness/contrast math itself, and cv2 isn't meaningfully
    faster than PIL at plain memory movement, only at the math. That's a
    real, safe win, not the order-of-magnitude one a raw cv2.LUT()-vs-
    numpy micro-benchmark suggests in isolation -- see the chat discussion
    for the multi-core-machine caveat and the GPU-shader alternative for
    an actually large win. Callers should treat any exception here as
    "fall back to apply_color_adjustments()".
    """
    cv2 = get_cv2()
    np = get_numpy()

    if saturation != 100:
        s = saturation / 100.0
        lr, lg, lb = _LUMA_R, _LUMA_G, _LUMA_B
        # Same 3x3 as _saturate_matrix's 3x4 (dropping its trailing zero
        # constant column -- cv2.transform has no offset term, and none
        # is needed here). Fed straight to cv2.transform as uint8: it
        # saturate-casts the result back to uint8 internally (verified
        # against the manual astype(float32)->clip->astype(uint8) route:
        # identical apart from the last-bit rounding direction, max 1/255
        # off), which skips two extra full-frame passes converting to and
        # from float32.
        matrix = np.array([
            [lr * (1 - s) + s, lg * (1 - s),     lb * (1 - s)],
            [lr * (1 - s),     lg * (1 - s) + s, lb * (1 - s)],
            [lr * (1 - s),     lg * (1 - s),     lb * (1 - s) + s],
        ], dtype=np.float32)
        rgb = cv2.transform(rgb, matrix)

    if brightness != 100:
        b = brightness / 100.0
        lut = np.clip(np.arange(256) * b, 0, 255).astype(np.uint8)
        rgb = cv2.LUT(rgb, lut)

    if contrast != 100:
        # Same pivot as apply_color_adjustments(): the mean of the
        # *luminance-weighted* grayscale version of the (already
        # saturation/brightness-adjusted) image, not a flat per-channel
        # average of R/G/B -- matches PIL's convert('L') mean so a static
        # image and an animated one with the same contrast value look the
        # same. Computed via the same cv2.transform() as a 1x3 matrix
        # (one luminance number out per pixel) rather than three separate
        # numpy multiplies, for the same reason as the saturation matrix
        # above. (This averages the unrounded per-pixel luminance and
        # rounds once at the end, rather than PIL's round-every-pixel-
        # then-average; the two differ by a small fraction of a unit at
        # most, not enough to change the rounded mean in practice.)
        gray = cv2.transform(rgb, np.array([[_LUMA_R, _LUMA_G, _LUMA_B]], dtype=np.float32))
        mean = round(float(gray.mean()))
        c = contrast / 100.0
        lut = np.clip(mean + (np.arange(256) - mean) * c, 0, 255).astype(np.uint8)
        rgb = cv2.LUT(rgb, lut)

    return rgb

def _process_animated_frame_fast(raw, w, h, saturation, brightness, contrast, target_w, target_h):
    """cv2-based replacement for the PIL block in
    _submit_animated_frame_processing's worker(): color-adjust (and
    resize, if needed) one animated frame's raw RGBA buffer straight from
    Qt, without a PIL round trip. Returns (rgba_bytes, width, height), or
    None if cv2 isn't installed or anything else goes wrong -- the caller
    falls back to the original PIL path in that case, so a missing cv2
    install degrades to the old speed instead of breaking playback."""
    try:
        cv2 = get_cv2()
        np = get_numpy()
        # .copy() so this is a normal writable, contiguous array -- raw is
        # an immutable bytes object, and frombuffer()'s view onto it isn't
        # writable, which some cv2 ops need.
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4)).copy()
        rgb = apply_color_adjustments_cv2(arr[:, :, :3], saturation, brightness, contrast)
        out = np.empty((h, w, 4), dtype=np.uint8)
        out[:, :, :3] = rgb
        # Always fully opaque, matching the PIL path above exactly: its
        # src.convert('RGB') drops the source alpha, and convert('RGBA')
        # coming back always fills alpha with 255 -- it never round-trips
        # the original values either. Carrying the real source alpha
        # through here instead would be a behavior change, not just a
        # speedup, so this keeps it byte-for-byte consistent instead.
        out[:, :, 3] = 255
        if target_w and target_h and (w != target_w or h != target_h):
            out = cv2.resize(out, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
        out = np.ascontiguousarray(out)
        return out.tobytes(), out.shape[1], out.shape[0]
    except Exception:
        return None


class GpuColorCorrector:
    """Renders anim_saturation/anim_brightness/anim_contrast on the GPU
    with a GLSL fragment shader -- the fastest of the three tiers this
    app now tries in order for animated gif/webp color adjustment: this,
    then apply_color_adjustments_cv2 (see _process_animated_frame_fast),
    then apply_color_adjustments (Pillow) as the final fallback. A
    texture upload plus one shader pass over the same pixels is fast
    enough to run synchronously on the GUI thread, so for the frames
    this succeeds on, neither of the other two tiers' work (the anim
    worker pool hop, the Qt signal round trip) happens at all -- see
    _render_animated_frame_gpu and _prefetch_ahead in ImageViewer.
    Nothing about the other two tiers is changed; they're still there,
    untouched, for whenever this returns None (unsupported driver, GL
    init failure, etc).

    Uses QOffscreenSurface + QOpenGLContext -- Qt's documented way to
    render with OpenGL without a visible window -- rather than
    QOpenGLWidget, which expects to be part of a shown widget hierarchy.
    A fresh QOpenGLTexture/QOpenGLFramebufferObject is created per call
    instead of trying to resize/reuse GL objects across frames of
    differing sizes; simpler and safer. (Whether that per-frame
    allocate/free is itself cheap enough to matter is driver-dependent --
    worth comparing playback smoothness against the cv2 tier alone on
    the actual target machine, the same way the cv2 tier's real-world
    speedup turned out smaller than a first estimate suggested.)

    One behavior difference from the other two tiers: contrast there
    pivots around the *current frame's* actual mean brightness (via
    Pillow's ImageStat / cv2.transform()+mean(), computed after
    saturation/brightness are applied); doing that on the GPU would need
    a separate reduction pass over the frame, which reintroduces
    per-frame overhead this tier exists to avoid. This shader pivots
    contrast at a fixed mid-gray (0.5) instead -- the standard real-time
    approximation -- so output only differs from the other two tiers
    when contrast != 100 *and* the frame's average brightness sits far
    from mid-gray.
    """

    _VERTEX_SRC = """
        attribute vec2 a_position;
        attribute vec2 a_texcoord;
        varying vec2 v_texcoord;
        void main() {
            v_texcoord = a_texcoord;
            gl_Position = vec4(a_position, 0.0, 1.0);
        }
    """

    _FRAGMENT_SRC = """
        uniform sampler2D u_texture;
        uniform float u_saturation;
        uniform float u_brightness;
        uniform float u_contrast;
        varying vec2 v_texcoord;
        void main() {
            vec3 color = texture2D(u_texture, v_texcoord).rgb;
            // Same luma weights and mix() blend as _saturate_matrix, and
            // clamped after each step just like apply_color_adjustments's
            // three separate matrix/LUT passes each are (PIL's point()
            // and cv2.LUT()/cv2.transform() all saturate to the valid
            // range before the next step runs). Clamping only once at
            // the very end -- the original version of this shader -- let
            // an extreme slider combination's intermediate overshoot
            // compound across steps instead of getting capped between
            // them, which could visibly diverge from the other two tiers
            // at combined extreme settings.
            float gray = dot(color, vec3(0.299, 0.587, 0.114));
            color = clamp(mix(vec3(gray), color, u_saturation), 0.0, 1.0);
            // Same plain multiply as the brightness LUT in
            // apply_color_adjustments.
            color = clamp(color * u_brightness, 0.0, 1.0);
            // Fixed mid-gray pivot -- see class docstring.
            color = clamp((color - 0.5) * u_contrast + 0.5, 0.0, 1.0);
            gl_FragColor = vec4(color, 1.0);
        }
    """

    _GL_TRIANGLE_STRIP = 0x0005

    def __init__(self):
        self._context = None
        self._surface = None
        self._program = None
        self._broken = False  # set once init fails, so we stop retrying every frame

    def _ensure_ready(self):
        if self._program is not None:
            return True
        if self._broken:
            return False
        try:
            surface = QOffscreenSurface()
            surface.create()
            if not surface.isValid():
                raise RuntimeError('오프스크린 surface 생성 실패')
            context = QOpenGLContext()
            if not context.create():
                raise RuntimeError('GL 컨텍스트 생성 실패')
            if not context.makeCurrent(surface):
                raise RuntimeError('makeCurrent 실패')
            try:
                program = QOpenGLShaderProgram()
                if not program.addShaderFromSourceCode(QOpenGLShader.Vertex, self._VERTEX_SRC):
                    raise RuntimeError(program.log())
                if not program.addShaderFromSourceCode(QOpenGLShader.Fragment, self._FRAGMENT_SRC):
                    raise RuntimeError(program.log())
                if not program.link():
                    raise RuntimeError(program.log())
            finally:
                context.doneCurrent()
            self._surface = surface
            self._context = context
            self._program = program
            return True
        except Exception as e:
            print(f"GPU 색보정 초기화 실패, 이후 프레임은 cv2/PIL 경로를 사용합니다: {e}")
            self._broken = True
            self._context = None
            self._surface = None
            self._program = None
            return False

    def adjust(self, qimage, saturation, brightness, contrast, target_w, target_h):
        """qimage: source frame, any QImage format. saturation/brightness/
        contrast: 1.0 = no change. Returns a QImage sized target_w x
        target_h with alpha forced fully opaque -- apply_color_adjustments
        and apply_color_adjustments_cv2 already lose per-pixel alpha the
        same way, so this matches their existing output -- or None if the
        GPU path isn't available, in which case the caller should fall
        back to the cv2/Pillow tiers."""
        if target_w <= 0 or target_h <= 0 or not self._ensure_ready():
            return None
        texture = None
        fbo = None
        try:
            if not self._context.makeCurrent(self._surface):
                return None
            texture = QOpenGLTexture(qimage.convertToFormat(QImage.Format_RGBA8888),
                                      QOpenGLTexture.DontGenerateMipMaps)
            texture.setMinificationFilter(QOpenGLTexture.Linear)
            texture.setMagnificationFilter(QOpenGLTexture.Linear)
            texture.setWrapMode(QOpenGLTexture.ClampToEdge)

            fbo = QOpenGLFramebufferObject(target_w, target_h)
            if not fbo.bind():
                return None
            self._context.functions().glViewport(0, 0, target_w, target_h)

            program = self._program
            program.bind()
            texture.bind(0)
            program.setUniformValue('u_texture', 0)
            program.setUniformValue('u_saturation', float(saturation))
            program.setUniformValue('u_brightness', float(brightness))
            program.setUniformValue('u_contrast', float(contrast))

            pos_loc = program.attributeLocation('a_position')
            uv_loc = program.attributeLocation('a_texcoord')
            # NDC corners for a full-viewport quad, paired with UVs chosen
            # so QImage's top row (row 0 of the buffer we just uploaded)
            # lands at the top of the quad (NDC y=+1); toImage() below
            # always flips OpenGL's bottom-up convention back to raster
            # order, so that same top content ends up back at row 0 of
            # the output QImage -- orientation round-trips correctly.
            # (Checked by hand-tracing the mapping, not by running it --
            # this sandbox has no GPU -- so it's worth a quick visual
            # check on an asymmetric test image on the real machine.)
            positions = [QVector2D(-1, -1), QVector2D(1, -1), QVector2D(-1, 1), QVector2D(1, 1)]
            texcoords = [QVector2D(0, 1), QVector2D(1, 1), QVector2D(0, 0), QVector2D(1, 0)]
            program.enableAttributeArray(pos_loc)
            program.setAttributeArray(pos_loc, positions)
            program.enableAttributeArray(uv_loc)
            program.setAttributeArray(uv_loc, texcoords)

            self._context.functions().glDrawArrays(self._GL_TRIANGLE_STRIP, 0, 4)

            program.disableAttributeArray(pos_loc)
            program.disableAttributeArray(uv_loc)
            texture.release()
            program.release()

            result = fbo.toImage()
            return result if not result.isNull() else None
        except Exception as e:
            print(f"GPU 프레임 색보정 실패, 이 프레임은 cv2/PIL 경로로 대체합니다: {e}")
            return None
        finally:
            if fbo is not None:
                fbo.release()
            if texture is not None:
                texture.destroy()
            if self._context is not None:
                self._context.doneCurrent()

    def shutdown(self):
        if self._context is None:
            return
        try:
            if self._surface is not None:
                self._context.makeCurrent(self._surface)
            self._program = None
        except Exception:
            pass
        finally:
            try:
                self._context.doneCurrent()
            except Exception:
                pass
            self._context = None
            self._surface = None


# The broken-file placeholder image (see _default_broken_pixmap), embedded
# as base64 instead of a separate file on disk -- a packaged (PyInstaller)
# build only has to ship the one exe/script, with nothing extra to add to
# the spec file and no path to resolve at runtime across dev vs. frozen
# modes. Re-encoded from the original PNG as an indexed (256-color)
# palette image, which this particular flat-color illustration compresses
# into extremely well (~150KB -> ~53KB) with no visible quality loss --
# full RGBA would have made this block more than 4x bigger for no visible
# benefit. Decoded once and cached; see _default_broken_pixmap.
_BROKEN_IMAGE_B64 = (
    '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'
    'SUVORK5CYII='
)


def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

def get_icon_path():
    possible_paths = [
        os.path.join(get_app_dir(), 'icon.ico'),
        os.path.join(getattr(sys, '_MEIPASS', ''), 'icon.ico') if hasattr(sys, '_MEIPASS') else '',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico'),
        os.path.join(os.path.dirname(get_app_dir()), 'icon.ico'),
    ]
    for path in possible_paths:
        if path and os.path.exists(path):
            return path
    return None

def get_frame_count(filepath):
    """Return a file's real animated-frame count via Pillow, or 0 if it
    isn't a multi-frame animation. Used for both gif and webp:
    QMovie.frameCount() is unreliable for many animated webp files (often
    reports 0), and for gif this is what tells a genuinely animated gif
    apart from a single-frame one -- a single-frame gif has nothing to
    animate, so it belongs on the normal static-image path instead of
    QMovie. (A QMovie whose one frame never advances again also never gets
    asked to redecode at a new size, so a later window resize can leave it
    stuck showing that frame at the old size -- routing it away from
    QMovie entirely sidesteps that instead of trying to patch around it.)"""
    try:
        Image = get_pil_image()
        with Image.open(filepath) as img:
            if getattr(img, 'is_animated', False):
                n = getattr(img, 'n_frames', 1)
                if n > 1:
                    return n
    except:
        pass
    return 0

class SingleApplication:
    def __init__(self, app_name="PekoviewerApp"):
        self.app_name = app_name
        self.socket = QLocalSocket()
        self.server = None
        self.file_received_callback = None

    def is_running(self):
        self.socket.connectToServer(self.app_name)
        # The already-running instance can only accept this connection once
        # its GUI thread is free -- e.g. it may be mid-decode of a large
        # animated webp frame right now. 30ms was too tight for that and
        # made this check false-negative under exactly that load. A real
        # "nothing is listening" case still fails almost immediately (a
        # refused connection isn't a timeout), so this doesn't slow down a
        # normal cold start.
        return self.socket.waitForConnected(1000)

    def start_server(self):
        self.server = QLocalServer()
        self.server.listen(self.app_name)
        self.server.newConnection.connect(self.on_new_connection)

    def send_message(self, message):
        if self.socket.state() == QLocalSocket.ConnectedState:
            self.socket.write(message.encode('utf-8'))
            # flush() alone doesn't guarantee the bytes actually left the
            # pipe -- Qt's own docs note the amount written depends on the
            # OS and say to use waitForBytesWritten() when not about to
            # return to an event loop, which is exactly this case (this
            # process calls sys.exit(0) right after send_message returns,
            # so it never does). Without this wait, disconnectFromServer()
            # right below could tear the pipe down before the message
            # actually left, silently dropping the file to open.
            self.socket.waitForBytesWritten(2000)
            self.socket.disconnectFromServer()

    def on_new_connection(self):
        socket = self.server.nextPendingConnection()
        if socket is None:
            return
        # The old fixed 30ms wait here regularly timed out before the
        # sender's bytes had arrived whenever this GUI thread happened to
        # be busy at that instant -- a large animated webp mid-frame-decode
        # is the common case -- which silently dropped the file-switch
        # request with no error and no retry. 3 seconds gives large
        # headroom over any realistic decode stall while keeping this as
        # the same plain, direct read the rest of this class already used.
        if socket.waitForReadyRead(3000):
            data = socket.readAll().data().decode('utf-8', errors='ignore')
            if self.file_received_callback and data:
                self.file_received_callback(data)
        socket.disconnectFromServer()

    def set_file_received_callback(self, callback):
        self.file_received_callback = callback

class Settings:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        app_dir = get_app_dir()
        self.settings_file = os.path.join(app_dir, 'pekoviewer_settings.json')
        
        if not os.path.exists(self.settings_file):
            self.data = self.default_settings()
            self.save()
        else:
            self.load()
    
    def load(self):
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        except:
            self.data = self.default_settings()
    
    def save(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except:
            pass
    
    def default_settings(self):
        return {
            'window_geometry': {
                'x': 777, 'y': 258, 'width': 595, 'height': 608
            },
            'zoom_quality': 'balanced',
            'show_filename': False,
            'background_color': '#2b2b2b',
            'fit_to_window': True,
            'snap_enabled': True,
            'snap_threshold': 20,
            'saturation': 100,
            'brightness': 100,
            'contrast': 100,
            'anim_saturation': 100,
            'anim_brightness': 100,
            'anim_contrast': 100,
            'remember_zip_position': False,
            'zip_position_history': {},
            'slideshow_interval': 3,
            'slideshow_mode': 'time',
            'slideshow_gif_loops': 2,
            'cache_size': 200,
            'cache_mb': 768,
            'preload_next': True,
            'preload_count': 3,
            'shortcuts': {
                'next_image': ['', ''],
                'prev_image': ['', ''],
                'zoom_in': ['', ''],
                'zoom_out': ['', ''],
                'toggle_actual_size': ['Tilt Left', ''],
                'toggle_fullscreen': ['Left Double Click', ''],
                'close_program': ['XButton1', ''],
                'show_image_list': ['Return', ''],
                'delete_image': ['', ''],
                'open_file': ['', ''],
                'slideshow': ['', ''],
            }
        }
    
    def get(self, key, default=None):
        return self.data.get(key, default)
    
    def set(self, key, value):
        self.data[key] = value
        self.save()

    def update_many(self, values):
        if values:
            self.data.update(values)
            self.save()

    def update_shortcuts_many(self, values):
        if values:
            self.data.setdefault('shortcuts', {}).update(values)
            self.save()

    def get_shortcuts(self, action):
        shortcuts = self.data.get('shortcuts', {})
        value = shortcuts.get(action, ['', ''])
        if isinstance(value, str):
            return [value, '']
        if isinstance(value, list):
            while len(value) < 2:
                value.append('')
            return value[:2]
        return ['', '']
    
    def set_shortcuts(self, action, shortcuts_list):
        if 'shortcuts' not in self.data:
            self.data['shortcuts'] = {}
        self.data['shortcuts'][action] = shortcuts_list
        self.save()

# PIL releases the GIL during its C-level decode/enhance work, so these
# worker threads genuinely run in parallel on multi-core machines. The old
# cap of 4 could bottleneck once the current image plus several preloaded
# neighbors are all in flight together; 8 gives more headroom on typical
# desktops while the floor of 2 and the cpu_count() scaling still protect
# low-core machines.
_DECODE_WORKER_COUNT = max(2, min(8, (os.cpu_count() or 4)))

# Animated-frame color processing gets its own small pool, separate from
# _executor above (which handles static-image decode/preload). Without this
# split, playing a color-adjusted gif/webp while neighboring images preload
# in the background makes both compete for the same workers -- the
# animation stalls waiting behind preload jobs and preload slows down too.
# Kept small (1-3) on purpose: only one frame is ever "live" per movie plus
# a couple of look-ahead frames, so extra workers here wouldn't speed up a
# single animation, they'd just take workers away from the shared pool.
_ANIM_WORKER_COUNT = max(1, min(3, (os.cpu_count() or 4) // 2))

# Upper bound on the *output* pixel count (target_w * target_h) the
# synchronous GPU animated-color-correction tier will attempt -- see
# _render_animated_frame_gpu. That tier does a full texture upload + FBO
# render + CPU readback on the GUI thread every call, so its cost scales
# with pixel count; past this it can itself take long enough to be a
# visible stall, which is worse than just taking the (async, off-thread)
# cv2/Pillow tiers below it. Set a bit above a typical 1440p fit-to-window
# target so ordinary playback is unaffected -- only frames clearly past
# that give up the GPU tier's latency advantage for the async tiers'
# never-blocks-the-UI guarantee.
_ANIM_GPU_MAX_PIXELS = 2560 * 1440

class ImageLoader:
    _shutdown = False
    # bmp/tif(f)/ico added on top of the original set -- Pillow already
    # handles all of them (no new dependency), and none of them are ever
    # animated here (see _load_animated_movie's ext check), so they go
    # straight through the same static-image decode path as png/jpg: the
    # QImageReader fast path with a PIL fallback, background-thread
    # decoding, and the existing cache. No change to per-frame/playback
    # cost for gif or webp.
    SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.gif', '.webp',
                          '.bmp', '.tif', '.tiff', '.ico'}
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=_DECODE_WORKER_COUNT)
    _anim_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_WORKER_COUNT)

    @staticmethod
    def is_supported(filename):
        return os.path.splitext(filename)[1].lower() in ImageLoader.SUPPORTED_FORMATS

    @staticmethod
    def load_image_data(filepath, saturation=100, brightness=100, contrast=100, max_size=None):
        # Fast path: native Qt decoding avoids Pillow RGB conversion and byte copies.
        try:
            if saturation == 100 and brightness == 100 and contrast == 100:
                reader = QImageReader(filepath)
                reader.setAutoTransform(True)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    src_size = reader.size()
                    if src_size.isValid() and src_size.width() > 0 and src_size.height() > 0:
                        reader.setScaledSize(src_size.scaled(
                            QSize(int(max_size[0]), int(max_size[1])), Qt.KeepAspectRatio))
                image = reader.read()
                if not image.isNull():
                    return image

            # Keep Pillow for the color-adjustment path so output behavior stays the same.
            Image = get_pil_image()
            with Image.open(filepath) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    # JPEG-only fast path: lets libjpeg decode at a reduced
                    # DCT scale instead of fully decoding every source pixel
                    # only to immediately throw most of them away in the
                    # thumbnail() resize below -- a no-op for every other
                    # format (PNG/WebP/GIF/...), so always safe to call.
                    # Without this, the saturation==100 branch above got a
                    # cheap scaled decode for free from
                    # QImageReader.setScaledSize(), but the moment any
                    # adjustment was non-default, this branch fully decoded
                    # a large JPEG at native resolution on every single
                    # navigation -- even though only a small fit-to-window
                    # preview was ever needed. This is the main reason
                    # image-to-image navigation felt much slower with
                    # saturation/brightness/contrast turned on than at
                    # defaults.
                    try:
                        src.draft('RGB', max_size)
                    except Exception:
                        pass
                img = src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    # BILINEAR here trades a little resample quality for real
                    # speed: this thumbnail gets scaled again by Qt to the
                    # exact viewport size right after (update_image_display),
                    # so LANCZOS's extra sharpness on this intermediate step
                    # was mostly being thrown away anyway.
                    resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                    img.thumbnail(max_size, resample)
                img = apply_color_adjustments(img, saturation, brightness, contrast)
                data = img.tobytes('raw', 'RGB')
                return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        except Exception as e:
            print(f"이미지 백그라운드 로드 오류: {e}")
        return None

    @staticmethod
    def load_pixmap(filepath, quality='balanced', saturation=100, brightness=100, contrast=100):
        image = ImageLoader.load_image_data(filepath, saturation, brightness, contrast)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        try:
            pixmap = QPixmap(filepath)
            return pixmap if not pixmap.isNull() else None
        except Exception:
            return None

    @staticmethod
    def load_thumbnail(filepath, size=(150, 150)):
        image = ImageLoader.load_image_data(filepath, max_size=size)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        return None

    @staticmethod
    @classmethod
    def shutdown_executor(cls):
        cls._shutdown = True
        try:
            cls._executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            cls._executor.shutdown(wait=True)
        except Exception:
            pass
        try:
            cls._anim_executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            cls._anim_executor.shutdown(wait=True)
        except Exception:
            pass

    @classmethod
    def restart_executor(cls):
        if cls._shutdown:
            cls._executor = concurrent.futures.ThreadPoolExecutor(max_workers=_DECODE_WORKER_COUNT)
            cls._anim_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_WORKER_COUNT)
            cls._shutdown = False

    @staticmethod
    def load_movie(filepath):
        try:
            movie = QMovie(filepath)
            if movie.isValid():
                return movie
        except Exception:
            pass
        return None


class CacheManager:
    def __init__(self, max_size=200, max_mb=768):
        self.max_size = max(20, int(max_size or 200))
        self.max_bytes = max(128, int(max_mb or 768)) * 1024 * 1024
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.lock = threading.RLock()

    @staticmethod
    def _cost(value):
        try:
            return max(1, value.width() * value.height() * 4)
        except Exception:
            return 1

    def get(self, key):
        with self.lock:
            value = self.cache.pop(key, None)
            if value is not None:
                self.cache[key] = value
            return value

    def put(self, key, value):
        if value is None:
            return
        cost = self._cost(value)
        with self.lock:
            old = self.cache.pop(key, None)
            if old is not None:
                self.cache_bytes -= self._cost(old)
            while self.cache and (len(self.cache) >= self.max_size or self.cache_bytes + cost > self.max_bytes):
                _, evicted = self.cache.popitem(last=False)
                self.cache_bytes -= self._cost(evicted)
            if cost <= self.max_bytes:
                self.cache[key] = value
                self.cache_bytes += cost

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.cache_bytes = 0


class ZipHandler:
    _thread_local = threading.local()
    _supported = ImageLoader.SUPPORTED_FORMATS

    @staticmethod
    def is_zip(filename):
        return filename.lower().endswith('.zip')

    @staticmethod
    def list_images(zip_path):
        images = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for info in zf.infolist():
                    if not info.is_dir() and os.path.splitext(info.filename)[1].lower() in ZipHandler._supported:
                        images.append(info.filename)
        except Exception as e:
            print(f"ZIP 목록 로드 오류: {e}")
        def natural_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
        images.sort(key=natural_key)
        return images

    @staticmethod
    def _get_zip(zip_path):
        handles = getattr(ZipHandler._thread_local, 'handles', None)
        if handles is None:
            handles = {}
            ZipHandler._thread_local.handles = handles
        key = os.path.abspath(zip_path)
        zf = handles.get(key)
        if zf is None or zf.fp is None:
            zf = zipfile.ZipFile(key, 'r')
            handles[key] = zf
        return zf

    @staticmethod
    def load_image_data(zip_path, filename, saturation=100, brightness=100, contrast=100, max_size=None):
        try:
            zf = ZipHandler._get_zip(zip_path)
            with zf.open(filename, 'r') as fp:
                data = fp.read()

            if saturation == 100 and brightness == 100 and contrast == 100:
                buffer = QBuffer()
                buffer.setData(QByteArray(data))
                buffer.open(QIODevice.ReadOnly)
                reader = QImageReader(buffer)
                reader.setAutoTransform(True)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    src_size = reader.size()
                    if src_size.isValid() and src_size.width() > 0 and src_size.height() > 0:
                        reader.setScaledSize(src_size.scaled(
                            QSize(int(max_size[0]), int(max_size[1])), Qt.KeepAspectRatio))
                image = reader.read()
                buffer.close()
                if not image.isNull():
                    return image

            Image = get_pil_image()
            with Image.open(BytesIO(data)) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    # See the matching comment in ImageLoader.load_image_data --
                    # same JPEG-only reduced-scale decode, same reason.
                    try:
                        src.draft('RGB', max_size)
                    except Exception:
                        pass
                img = src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                    img.thumbnail(max_size, resample)
                img = apply_color_adjustments(img, saturation, brightness, contrast)
                raw = img.tobytes('raw', 'RGB')
                return QImage(raw, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        except Exception as e:
            print(f"ZIP 이미지 백그라운드 로드 오류: {e}")
        return None

    @staticmethod
    def load_image_from_zip(zip_path, filename, saturation=100, brightness=100, contrast=100):
        image = ZipHandler.load_image_data(zip_path, filename, saturation, brightness, contrast)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        return None


class ImageLoadBridge(QObject):
    loaded = pyqtSignal(int, str, object, bool)
    animated_frame = pyqtSignal(int, int, object)
    hq_resample = pyqtSignal(int, object)

class ImageListDialog(QDialog):
    def __init__(self, image_list, current_index, parent=None, current_zip=None):
        super().__init__(parent)
        self.image_list = image_list
        self.current_index = current_index
        self.selected_index = current_index
        self.current_zip = current_zip
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle('이미지 목록')
        self.setModal(True)
        self.setMinimumSize(400, 500)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QListWidget { background-color: #3c3c3c; color: white; border: 1px solid #555; }
            QListWidget::item:selected { background-color: #4a90d9; }
            QLabel { color: white; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px; }
            QPushButton:hover { background-color: #4c4c4c; }
        """)
        layout = QVBoxLayout(self)
        self.preview_label = QLabel('이미지를 선택하세요')
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(200)
        self.preview_label.setStyleSheet("border: 1px solid #555; background-color: #3c3c3c;")
        layout.addWidget(self.preview_label)
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListView.ListMode)
        self.list_widget.setSpacing(2)
        for i, image_path in enumerate(self.image_list):
            display_name = os.path.basename(image_path)
            item = QListWidgetItem(display_name)
            item.setData(Qt.UserRole, i)
            self.list_widget.addItem(item)
        self.list_widget.setCurrentRow(self.current_index)
        self.list_widget.itemDoubleClicked.connect(self.on_double_click)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        layout.addWidget(self.list_widget)
        self.show_preview(self.current_index)
        button_layout = QHBoxLayout()
        select_button = QPushButton('선택')
        select_button.clicked.connect(self.accept)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(select_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def on_item_clicked(self, item):
        self.selected_index = item.data(Qt.UserRole)
        self.show_preview(self.selected_index)
    
    def on_double_click(self, item):
        self.selected_index = item.data(Qt.UserRole)
        self.accept()
    
    def show_preview(self, index):
        if index < 0 or index >= len(self.image_list):
            return
        self.preview_label.setText('로딩 중...')
        try:
            if self.current_zip:
                pixmap = ZipHandler.load_image_from_zip(self.current_zip, self.image_list[index])
            else:
                pixmap = ImageLoader.load_thumbnail(self.image_list[index])
            if pixmap and not pixmap.isNull():
                scaled = pixmap.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.preview_label.setPixmap(scaled)
        except:
            self.preview_label.setText('미리보기 불가')
    
    def get_selected_index(self):
        return self.selected_index

class ShortcutSettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.shortcut_buttons = {}
        self.capturing = False
        self.current_action = None
        self.current_slot = 0
        self.capture_timer = QTimer()
        self.capture_timer.setSingleShot(True)
        self.capture_timer.timeout.connect(self.finish_capture)
        self.captured_keys = []
        self.init_ui()
        self.load_shortcuts()
    
    def init_ui(self):
        self.setWindowTitle('단축키 설정')
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QLabel { color: white; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px 10px; }
            QPushButton:hover { background-color: #4c4c4c; }
            QGroupBox { color: white; border: 1px solid #555; margin-top: 10px; }
        """)
        layout = QVBoxLayout(self)
        info_label = QLabel('버튼 클릭 후 1초 동안 입력한 모든 키/마우스 버튼이 단축키로 설정됩니다.\n더블클릭: Left Double Click / Right Double Click\nESC: 삭제')
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        actions = [
            ('next_image', '다음 이미지'), ('prev_image', '이전 이미지'),
            ('toggle_fullscreen', '전체화면 토글'), ('close_program', '프로그램 닫기'),
            ('show_image_list', '이미지 목록 표시'), ('zoom_in', '확대'),
            ('zoom_out', '축소'), ('toggle_actual_size', '실제 크기/창 크기 토글'),
            ('delete_image', '삭제'), ('open_file', '열기'),
            ('slideshow', '슬라이드쇼'),
        ]
        for action_key, action_name in actions:
            group = QGroupBox(action_name)
            group_layout = QHBoxLayout()
            button1 = QPushButton('단축키 1')
            button1.setMinimumWidth(120)
            button1.clicked.connect(lambda checked, k=action_key, s=0, b=button1: self.start_capture(k, s, b))
            button2 = QPushButton('단축키 2')
            button2.setMinimumWidth(120)
            button2.clicked.connect(lambda checked, k=action_key, s=1, b=button2: self.start_capture(k, s, b))
            self.shortcut_buttons[action_key] = [button1, button2]
            group_layout.addWidget(button1)
            group_layout.addWidget(button2)
            group.setLayout(group_layout)
            layout.addWidget(group)
        button_layout = QHBoxLayout()
        reset_button = QPushButton('기본값 복원')
        reset_button.clicked.connect(self.reset_defaults)
        button_layout.addWidget(reset_button)
        button_layout.addStretch()
        save_button = QPushButton('저장')
        save_button.clicked.connect(self.save_shortcuts)
        button_layout.addWidget(save_button)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def load_shortcuts(self):
        actions = ['next_image', 'prev_image', 'toggle_fullscreen', 'close_program',
                  'show_image_list', 'zoom_in', 'zoom_out', 'toggle_actual_size',
                  'delete_image', 'open_file', 'slideshow']
        for action in actions:
            shortcuts = self.settings.get_shortcuts(action)
            if action in self.shortcut_buttons:
                for i, button in enumerate(self.shortcut_buttons[action]):
                    text = shortcuts[i] if i < len(shortcuts) and shortcuts[i] else '없음'
                    button.setText(text)
    
    def start_capture(self, action_key, slot, button):
        if self.capturing:
            return
        self.capturing = True
        self.current_action = action_key
        self.current_slot = slot
        self.captured_keys = []
        button.setText('입력 중... (1초)')
        button.setStyleSheet("background-color: #4a90d9; color: white; border: 1px solid #555; padding: 5px 10px;")
        self.grabKeyboard()
        self.setFocus()
        self.capture_timer.start(1000)
    
    def finish_capture(self):
        if self.capturing and self.current_action:
            if self.captured_keys:
                shortcut_text = self.captured_keys[0]
                self.shortcut_buttons[self.current_action][self.current_slot].setText(shortcut_text)
            else:
                self.shortcut_buttons[self.current_action][self.current_slot].setText('없음')
            self.shortcut_buttons[self.current_action][self.current_slot].setStyleSheet("")
            self.capturing = False
            self.current_action = None
            self.current_slot = 0
            self.captured_keys = []
            self.releaseKeyboard()
    
    def keyPressEvent(self, event):
        if self.capturing and self.current_action:
            key = event.key()
            modifiers = event.modifiers()
            if key == Qt.Key_Escape:
                self.shortcut_buttons[self.current_action][self.current_slot].setText('없음')
                self.shortcut_buttons[self.current_action][self.current_slot].setStyleSheet("")
                self.capture_timer.stop()
                self.capturing = False
                self.current_action = None
                self.current_slot = 0
                self.captured_keys = []
                self.releaseKeyboard()
                return
            key_sequence = QKeySequence(modifiers | key).toString()
            if key_sequence and key_sequence not in self.captured_keys:
                self.captured_keys.append(key_sequence)
        super().keyPressEvent(event)
    
    def wheelEvent(self, event):
        if self.capturing and self.current_action:
            dx = event.angleDelta().x()
            if dx != 0:
                # Match the physical tilt direction used by the viewer:
                # positive Qt horizontal delta corresponds to physical Tilt Left.
                button_text = 'Tilt Left' if dx > 0 else 'Tilt Right'
                if button_text not in self.captured_keys:
                    self.captured_keys.append(button_text)
                event.accept()
                return
        super().wheelEvent(event)

    def mousePressEvent(self, event):
        if self.capturing and self.current_action:
            button = event.button()
            mouse_buttons = {
                Qt.LeftButton: 'Left Click',
                Qt.RightButton: 'Right Click',
                Qt.MiddleButton: 'Middle Click',
                Qt.XButton1: 'XButton1',
                Qt.XButton2: 'XButton2'
            }
            if button in mouse_buttons:
                button_text = mouse_buttons[button]
                if button_text not in self.captured_keys:
                    self.captured_keys.append(button_text)
        super().mousePressEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        if self.capturing and self.current_action:
            if event.button() == Qt.LeftButton:
                if 'Left Double Click' not in self.captured_keys:
                    self.captured_keys.insert(0, 'Left Double Click')
            elif event.button() == Qt.RightButton:
                if 'Right Double Click' not in self.captured_keys:
                    self.captured_keys.insert(0, 'Right Double Click')
        super().mouseDoubleClickEvent(event)
    
    def reset_defaults(self):
        defaults = self.settings.default_settings()['shortcuts']
        for action, shortcuts in defaults.items():
            if action in self.shortcut_buttons:
                for i, button in enumerate(self.shortcut_buttons[action]):
                    text = shortcuts[i] if i < len(shortcuts) and shortcuts[i] else '없음'
                    button.setText(text)
    
    def save_shortcuts(self):
        values = {}
        for action, buttons in self.shortcut_buttons.items():
            shortcuts = [buttons[0].text(), buttons[1].text()]
            values[action] = [s if s != '없음' else '' for s in shortcuts]
        self.settings.update_shortcuts_many(values)
        self.accept()

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.init_ui()
        self.load_settings()
    
    def init_ui(self):
        self.setWindowTitle('설정')
        self.setModal(True)
        self.setMinimumWidth(450)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: #ffffff; }
            QGroupBox { color: #ffffff; border: 1px solid #555; margin-top: 10px; }
            QLabel { color: #ffffff; }
            QCheckBox { color: #ffffff; }
            QComboBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 3px; }
            QComboBox QAbstractItemView { background-color: #3c3c3c; color: #ffffff; selection-background-color: #4a90d9; }
            QSpinBox { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 3px; }
            QSlider::groove:horizontal { height: 8px; background: #555; border-radius: 4px; }
            QSlider::handle:horizontal { width: 24px; height: 24px; margin: -8px 0; background: #4a90d9; border-radius: 12px; }
            QSlider::handle:horizontal:hover { background: #6aa8e8; }
            QPushButton { background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; padding: 5px 10px; }
            QPushButton:hover { background-color: #4c4c4c; }
        """)
        layout = QVBoxLayout(self)
        
        display_group = QGroupBox('이미지 표시')
        display_layout = QFormLayout()
        self.zoom_quality = QComboBox()
        self.zoom_quality.addItem('속도 우선', 'speed')
        self.zoom_quality.addItem('균형', 'balanced')
        self.zoom_quality.addItem('품질 우선', 'quality')
        display_layout.addRow('확대/축소 품질:', self.zoom_quality)
        self.show_filename = QCheckBox('파일명 표시')
        display_layout.addRow('', self.show_filename)
        self.fit_to_window = QCheckBox('창에 맞추기')
        display_layout.addRow('', self.fit_to_window)

        self.preload_enabled = QCheckBox('주변 이미지 미리 로딩')
        display_layout.addRow('', self.preload_enabled)
        self.preload_count = QComboBox()
        for count, label in [(0, '사용 안 함'), (1, '앞/뒤 1장'), (2, '앞/뒤 2장'),
                             (3, '앞/뒤 3장'), (5, '앞/뒤 5장'), (10, '앞/뒤 10장')]:
            self.preload_count.addItem(label, count)
        display_layout.addRow('미리 로딩 범위:', self.preload_count)
        display_group.setLayout(display_layout)
        layout.addWidget(display_group)

        zip_group = QGroupBox('압축 파일')
        zip_layout = QFormLayout()
        self.remember_zip_position = QCheckBox('마지막으로 본 위치 기억 (최근 30개 파일)')
        zip_layout.addRow('', self.remember_zip_position)
        zip_note = QLabel('켜두면 압축 파일을 다시 열었을 때 마지막으로 보던 이미지부터 이어서 보여줍니다.')
        zip_note.setWordWrap(True)
        zip_layout.addRow('', zip_note)
        zip_group.setLayout(zip_layout)
        layout.addWidget(zip_group)
        
        static_adjust_group = QGroupBox('정지 이미지 조절')
        static_adjust_layout = QFormLayout()

        self.saturation_slider = QSlider(Qt.Horizontal)
        self.saturation_slider.setRange(0, 200)
        self.saturation_slider.setValue(100)
        self.saturation_label = QLabel('100%')
        saturation_row = QHBoxLayout()
        saturation_row.addWidget(self.saturation_slider)
        saturation_row.addWidget(self.saturation_label)
        static_adjust_layout.addRow('채도:', saturation_row)

        self.brightness_slider = QSlider(Qt.Horizontal)
        self.brightness_slider.setRange(0, 200)
        self.brightness_slider.setValue(100)
        self.brightness_label = QLabel('100%')
        brightness_row = QHBoxLayout()
        brightness_row.addWidget(self.brightness_slider)
        brightness_row.addWidget(self.brightness_label)
        static_adjust_layout.addRow('밝기:', brightness_row)

        self.contrast_slider = QSlider(Qt.Horizontal)
        self.contrast_slider.setRange(0, 200)
        self.contrast_slider.setValue(100)
        self.contrast_label = QLabel('100%')
        contrast_row = QHBoxLayout()
        contrast_row.addWidget(self.contrast_slider)
        contrast_row.addWidget(self.contrast_label)
        static_adjust_layout.addRow('명도/대비:', contrast_row)

        reset_adjust_button = QPushButton('정지 이미지 조절 초기화')
        reset_adjust_button.clicked.connect(self.reset_adjustments)
        static_adjust_layout.addRow('', reset_adjust_button)

        static_adjust_group.setLayout(static_adjust_layout)
        layout.addWidget(static_adjust_group)

        anim_adjust_group = QGroupBox('움직이는 이미지 조절 (GIF·애니메이션 WebP)')
        anim_adjust_layout = QFormLayout()

        self.anim_saturation_slider = QSlider(Qt.Horizontal)
        self.anim_saturation_slider.setRange(0, 200)
        self.anim_saturation_slider.setValue(100)
        self.anim_saturation_label = QLabel('100%')
        anim_saturation_row = QHBoxLayout()
        anim_saturation_row.addWidget(self.anim_saturation_slider)
        anim_saturation_row.addWidget(self.anim_saturation_label)
        anim_adjust_layout.addRow('채도:', anim_saturation_row)

        self.anim_brightness_slider = QSlider(Qt.Horizontal)
        self.anim_brightness_slider.setRange(0, 200)
        self.anim_brightness_slider.setValue(100)
        self.anim_brightness_label = QLabel('100%')
        anim_brightness_row = QHBoxLayout()
        anim_brightness_row.addWidget(self.anim_brightness_slider)
        anim_brightness_row.addWidget(self.anim_brightness_label)
        anim_adjust_layout.addRow('밝기:', anim_brightness_row)

        self.anim_contrast_slider = QSlider(Qt.Horizontal)
        self.anim_contrast_slider.setRange(0, 200)
        self.anim_contrast_slider.setValue(100)
        self.anim_contrast_label = QLabel('100%')
        anim_contrast_row = QHBoxLayout()
        anim_contrast_row.addWidget(self.anim_contrast_slider)
        anim_contrast_row.addWidget(self.anim_contrast_label)
        anim_adjust_layout.addRow('명도/대비:', anim_contrast_row)

        reset_anim_adjust_button = QPushButton('움직이는 이미지 조절 초기화')
        reset_anim_adjust_button.clicked.connect(self.reset_anim_adjustments)
        anim_adjust_layout.addRow('', reset_anim_adjust_button)

        anim_adjust_group.setLayout(anim_adjust_layout)
        layout.addWidget(anim_adjust_group)

        apply_button = QPushButton('현재 이미지에 즉시 적용')
        apply_button.clicked.connect(self.apply_immediately)
        layout.addWidget(apply_button)
        
        snap_group = QGroupBox('창 자석 기능')
        snap_layout = QFormLayout()
        self.snap_enabled = QCheckBox('화면 가장자리에 달라붙기')
        snap_layout.addRow('', self.snap_enabled)
        self.snap_threshold = QSpinBox()
        self.snap_threshold.setRange(5, 50)
        self.snap_threshold.setSuffix(' 픽셀')
        snap_layout.addRow('자석 작동 거리:', self.snap_threshold)
        snap_group.setLayout(snap_layout)
        layout.addWidget(snap_group)
        
        slideshow_group = QGroupBox('슬라이드쇼')
        slideshow_layout = QFormLayout()
        self.slideshow_mode = QComboBox()
        self.slideshow_mode.addItem('시간 기반', 'time')
        self.slideshow_mode.addItem('GIF 재생 횟수', 'loop')
        slideshow_layout.addRow('모드:', self.slideshow_mode)
        self.slideshow_interval = QSpinBox()
        self.slideshow_interval.setRange(1, 60)
        self.slideshow_interval.setSuffix(' 초')
        slideshow_layout.addRow('시간 간격:', self.slideshow_interval)
        self.slideshow_gif_loops = QSpinBox()
        self.slideshow_gif_loops.setRange(1, 10)
        self.slideshow_gif_loops.setSuffix(' 회')
        slideshow_layout.addRow('GIF 재생 횟수:', self.slideshow_gif_loops)
        slideshow_group.setLayout(slideshow_layout)
        layout.addWidget(slideshow_group)

        error_group = QGroupBox('오류 처리')
        error_layout = QFormLayout()
        error_note = QLabel('직접 이동 중 파일을 읽을 수 없으면 기본 대체 이미지가 표시됩니다.\n슬라이드쇼 중에는 대신 자동으로 다음 이미지로 건너뜁니다.')
        error_note.setWordWrap(True)
        error_layout.addRow('', error_note)
        error_group.setLayout(error_layout)
        layout.addWidget(error_group)
        
        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel('배경색:'))
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self.choose_color)
        color_layout.addWidget(self.color_button)
        layout.addLayout(color_layout)
        
        button_layout = QHBoxLayout()
        save_button = QPushButton('저장')
        save_button.clicked.connect(self.save_settings)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        
        self.saturation_slider.valueChanged.connect(
            lambda v: self.saturation_label.setText(f'{v}%'))
        self.brightness_slider.valueChanged.connect(
            lambda v: self.brightness_label.setText(f'{v}%'))
        self.contrast_slider.valueChanged.connect(
            lambda v: self.contrast_label.setText(f'{v}%'))
        self.anim_saturation_slider.valueChanged.connect(
            lambda v: self.anim_saturation_label.setText(f'{v}%'))
        self.anim_brightness_slider.valueChanged.connect(
            lambda v: self.anim_brightness_label.setText(f'{v}%'))
        self.anim_contrast_slider.valueChanged.connect(
            lambda v: self.anim_contrast_label.setText(f'{v}%'))
    
    def reset_adjustments(self):
        self.saturation_slider.setValue(100)
        self.brightness_slider.setValue(100)
        self.contrast_slider.setValue(100)
        self.apply_immediately()

    def reset_anim_adjustments(self):
        self.anim_saturation_slider.setValue(100)
        self.anim_brightness_slider.setValue(100)
        self.anim_contrast_slider.setValue(100)
        self.apply_immediately()

    def apply_immediately(self):
        parent = self.parent()
        if parent and hasattr(parent, 'apply_image_adjustments'):
            parent.apply_image_adjustments(
                self.saturation_slider.value(),
                self.brightness_slider.value(),
                self.contrast_slider.value(),
                self.anim_saturation_slider.value(),
                self.anim_brightness_slider.value(),
                self.anim_contrast_slider.value()
            )
    
    def load_settings(self):
        quality = self.settings.get('zoom_quality', 'balanced')
        index = self.zoom_quality.findData(quality)
        if index >= 0:
            self.zoom_quality.setCurrentIndex(index)
        self.show_filename.setChecked(self.settings.get('show_filename', False))
        self.fit_to_window.setChecked(self.settings.get('fit_to_window', True))
        self.preload_enabled.setChecked(self.settings.get('preload_next', True))
        preload_count = int(self.settings.get('preload_count', 3))
        preload_index = self.preload_count.findData(preload_count)
        if preload_index < 0:
            preload_index = self.preload_count.findData(3)
        self.preload_count.setCurrentIndex(preload_index)
        self.saturation_slider.setValue(self.settings.get('saturation', 100))
        self.brightness_slider.setValue(self.settings.get('brightness', 100))
        self.contrast_slider.setValue(self.settings.get('contrast', 100))
        self.anim_saturation_slider.setValue(self.settings.get('anim_saturation', 100))
        self.anim_brightness_slider.setValue(self.settings.get('anim_brightness', 100))
        self.anim_contrast_slider.setValue(self.settings.get('anim_contrast', 100))
        self.snap_enabled.setChecked(self.settings.get('snap_enabled', True))
        self.snap_threshold.setValue(self.settings.get('snap_threshold', 20))
        mode = self.settings.get('slideshow_mode', 'time')
        index = self.slideshow_mode.findData(mode)
        if index >= 0:
            self.slideshow_mode.setCurrentIndex(index)
        self.slideshow_interval.setValue(self.settings.get('slideshow_interval', 3))
        self.slideshow_gif_loops.setValue(self.settings.get('slideshow_gif_loops', 2))
        self.remember_zip_position.setChecked(self.settings.get('remember_zip_position', False))
        self.current_color = self.settings.get('background_color', '#2b2b2b')
        self.update_color_button()
    
    def choose_color(self):
        color = QColorDialog.getColor()
        if color.isValid():
            self.current_color = color.name()
            self.update_color_button()
    
    def update_color_button(self):
        self.color_button.setStyleSheet(f"background-color: {self.current_color}; color: white;")
        self.color_button.setText(self.current_color)
    
    def save_settings(self):
        self.settings.update_many({
            'zoom_quality': self.zoom_quality.currentData(),
            'show_filename': self.show_filename.isChecked(),
            'fit_to_window': self.fit_to_window.isChecked(),
            'preload_next': self.preload_enabled.isChecked(),
            'preload_count': self.preload_count.currentData(),
            'saturation': self.saturation_slider.value(),
            'brightness': self.brightness_slider.value(),
            'contrast': self.contrast_slider.value(),
            'anim_saturation': self.anim_saturation_slider.value(),
            'anim_brightness': self.anim_brightness_slider.value(),
            'anim_contrast': self.anim_contrast_slider.value(),
            'snap_enabled': self.snap_enabled.isChecked(),
            'snap_threshold': self.snap_threshold.value(),
            'slideshow_mode': self.slideshow_mode.currentData(),
            'slideshow_interval': self.slideshow_interval.value(),
            'slideshow_gif_loops': self.slideshow_gif_loops.value(),
            'remember_zip_position': self.remember_zip_position.isChecked(),
            'background_color': self.current_color,
        })
        self.accept()

class PanLabel(QLabel):
    """Image label with reliable left-drag panning.

    The pan starts only after a small movement threshold so a normal
    left double-click can still trigger the fullscreen shortcut.
    """
    def __init__(self, viewer):
        super().__init__()
        self.viewer = viewer
        self._pressed = False
        self._panning = False
        self._press_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.viewer._can_pan_image():
            self._pressed = True
            self._panning = False
            self._press_pos = QPoint(event.globalPos())
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed and self._press_pos is not None:
            delta = QPoint(event.globalPos()) - self._press_pos
            if not self._panning and (abs(delta.x()) >= 4 or abs(delta.y()) >= 4):
                if self.viewer._start_image_pan(self._press_pos):
                    self._panning = True
            if self._panning:
                self.viewer._move_image_pan(event.globalPos())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._pressed:
            was_panning = self._panning
            self._pressed = False
            self._panning = False
            self._press_pos = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            if was_panning:
                self.viewer._end_image_pan()
                event.accept()
                return
            # No movement: let the viewer handle the click/double-click.
            # Releasing here prevents the frameless-window drag from starting.
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._pressed = False
            self._panning = False
            self._press_pos = None
            try:
                self.releaseMouse()
            except Exception:
                pass
            self.viewer.check_mouse_shortcut('Left Double Click')
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ImageViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.cache_manager = CacheManager(self.settings.get('cache_size', 200), self.settings.get('cache_mb', 768))
        self.load_bridge = ImageLoadBridge()
        self.load_bridge.loaded.connect(self._on_background_loaded)
        self.load_bridge.animated_frame.connect(self._on_animated_frame_ready)
        self.load_bridge.hq_resample.connect(self._on_hq_resample_ready)
        self.load_generation = 0
        self.loading_keys = set()
        # Cache the expensive color-adjusted source separately from the
        # display-size cache. This prevents repeated Pillow work when navigating.
        self._adjusted_image_cache = {}
        self._adjusted_image_cache_order = []
        self._adjusted_image_cache_limit = 24
        self._source_image_cache = {}
        self._source_image_cache_order = []
        self._source_image_cache_limit = 12
        self.load_retry_counts = {}
        self.preload_enabled = self.settings.get('preload_next', True)
        self.preload_count = max(0, min(10, int(self.settings.get('preload_count', 3))))
        self.slideshow = QTimer()
        self.slideshow.timeout.connect(self.next_image)
        self.slideshow_playing = False
        self.slideshow_mode = 'time'
        self.slideshow_fail_streak = 0
        self.gif_loop_count = 0
        self.gif_max_loops = 2
        self.gif_frame_connected = False
        self.gif_last_frame = -1
        self.current_index = 0
        self.image_list = []
        self.current_zip = None
        # Per-archive "last file viewed" memory (see _remember_zip_position
        # and load_zip), capped at 30 archives -- an OrderedDict so
        # move_to_end/popitem can implement that cap as LRU, same pattern
        # as animated_frame_cache below. Loaded from Settings here;
        # written back only on close (see ImageViewer.save_settings) since
        # writing the settings file on every single image switch would be
        # excessive disk I/O for something that only needs to survive a
        # normal quit.
        self.zip_position_history = OrderedDict(self.settings.get('zip_position_history', {}) or {})
        self.zoom_factor = 1.0
        self.fit_to_window = True
        self.current_movie = None
        # Backing QBuffer for a movie built from in-memory bytes (a zip
        # entry, since QMovie can't read a zip path directly). Must be kept
        # alive for as long as the movie is; stop_current_movie() closes it.
        self.current_movie_buffer = None
        self.current_movie_original_size = None
        self.current_movie_generation = 0
        self.animated_frame_cache = OrderedDict()
        self.animated_frame_cache_limit = 24
        self.current_movie_frame = -1
        # A second, unstarted QMovie built from the exact same source as
        # current_movie, used only to jumpToFrame() a few frames ahead and
        # hand those frames to the anim worker pool for color processing
        # *before* playback reaches them. It decodes through the same Qt
        # plugin with the same setScaledSize(), so prefetched frames are
        # pixel-identical to what the live movie would have produced --
        # unlike re-decoding with Pillow, which could scale slightly
        # differently. Keys already in animated_frame_cache/in-flight are
        # skipped, so this only ever does extra work that would otherwise
        # have been done later anyway, just earlier.
        self.prefetch_movie = None
        self.prefetch_buffer = None
        self.prefetch_frame_count = None
        self.anim_lookahead = 2
        self.animated_inflight_keys = set()
        # Renders anim_saturation/anim_brightness/anim_contrast on the GPU
        # instead of the cv2/Pillow tiers below -- see GpuColorCorrector
        # and _render_animated_frame_gpu. One instance persists for the
        # window's lifetime (GL context is created lazily on first use);
        # torn down in closeEvent.
        self.gl_color_corrector = GpuColorCorrector()
        # The size the current animated frame should actually appear at on
        # screen (post zoom/fit). May be larger than
        # current_movie_original_size when zoomed in -- see
        # _anim_decode_size/_apply_anim_scaled_size for why the movies
        # themselves are capped at the original resolution instead of
        # being asked to decode at this size directly.
        self.current_movie_target_size = None
        self.current_pixmap = None
        self.original_pixmap = None
        self.is_loading = False
        self._default_broken_pixmap_cache = None
        self.dragging = False
        self.drag_start_pos = None
        # Image panning: when zoomed beyond the viewport, drag the image itself.
        # Only scrollbar offsets change during a pan; the pixmap is never re-scaled.
        self.panning = False
        self.pan_start_pos = None
        self.pan_start_h = 0
        self.pan_start_v = 0
        self.window_start_pos = None
        self.resizing = False
        self.resize_start_pos = None
        self.resize_start_size = None
        self.resize_region = None
        self.resize_margin = 12
        self.cursor_hidden = False
        self.cursor_hide_timer = QTimer()
        self.cursor_hide_timer.setSingleShot(True)
        self.cursor_hide_timer.timeout.connect(self.hide_cursor)
        self._display_update_timer = QTimer(self)
        self._display_update_timer.setSingleShot(True)
        self._display_update_timer.timeout.connect(self.update_image_display)
        # Fires once window-resize activity has been quiet for a bit -- see
        # resizeEvent and _apply_high_quality_resample. Kept separate from
        # _display_update_timer above (which redraws immediately, cheaply,
        # on every resize event for responsiveness) so the expensive
        # Lanczos re-render only happens once, after resizing settles.
        self._hq_resample_timer = QTimer(self)
        self._hq_resample_timer.setSingleShot(True)
        self._hq_resample_timer.timeout.connect(self._apply_high_quality_resample)
        self._hq_resample_inflight = False
        self.init_ui()
        self.load_settings()
        self.setup_icon()
        self.slideshow.setInterval(self.settings.get('slideshow_interval', 3) * 1000)
    
    def setup_icon(self):
        icon_path = get_icon_path()
        if icon_path:
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)
            app = QApplication.instance()
            if app:
                app.setWindowIcon(icon)
    
    def showEvent(self, event):
        super().showEvent(event)
        icon_path = get_icon_path()
        if icon_path:
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)
            if self.windowHandle():
                self.windowHandle().setIcon(icon)
        self.reset_cursor_timer()
    
    def hide_cursor(self):
        # Never hide mid-interaction: losing the cursor while actively
        # resizing/dragging/panning would be disorienting.
        if self.cursor_hidden or self.dragging or self.resizing or self.panning:
            return
        self.setCursor(Qt.BlankCursor)
        self.cursor_hidden = True
    
    def show_cursor(self):
        if self.cursor_hidden:
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            self.cursor_hidden = False
    
    def reset_cursor_timer(self):
        # Auto-hide-after-idle now applies in windowed mode too, not just fullscreen.
        self.cursor_hide_timer.start(2000)
    
    def bring_to_front(self):
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(100, self.force_foreground)
    
    def force_foreground(self):
        try:
            hwnd = int(self.winId())
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
            cur_thread = kernel32.GetCurrentThreadId()
            if cur_thread != fg_thread:
                user32.AttachThreadInput(cur_thread, fg_thread, True)
            user32.ShowWindow(hwnd, 9)
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0002 | 0x0001)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            if cur_thread != fg_thread:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
        except:
            pass
    
    def snap_to_edge(self, pos):
        if not self.settings.get('snap_enabled', True):
            return pos
        threshold = self.settings.get('snap_threshold', 20)
        screen = QApplication.primaryScreen().availableGeometry()
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        if abs(x - screen.left()) < threshold:
            x = screen.left()
        if abs((x + w) - screen.right()) < threshold:
            x = screen.right() - w
        if abs(y - screen.top()) < threshold:
            y = screen.top()
        if abs((y + h) - screen.bottom()) < threshold:
            y = screen.bottom() - h
        return QPoint(x, y)
    
    def apply_image_adjustments(self, saturation, brightness, contrast,
                                 anim_saturation, anim_brightness, anim_contrast):
        self.settings.update_many({
            'saturation': saturation,
            'brightness': brightness,
            'contrast': contrast,
            'anim_saturation': anim_saturation,
            'anim_brightness': anim_brightness,
            'anim_contrast': anim_contrast,
        })
        # No cache_manager.clear() here: every cache key already includes
        # saturation/brightness/contrast (see _cache_key / _animated_cache_key),
        # so entries made under the old values simply stop being matched
        # instead of needing eviction -- and leaving them in place means
        # flipping back to a value used earlier (or an image already
        # processed at the new one) can still hit the cache instead of
        # paying full decode+adjust again.
        if self.image_list:
            self.show_current_image()
    
    def init_ui(self):
        self.setWindowTitle('Pekoviewer')
        self.setMinimumSize(200, 150)
        self.setAcceptDrops(True)
        self.setWindowFlags(Qt.FramelessWindowHint)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.scroll_area)
        self.image_label = PanLabel(self)
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(100, 100)
        self.image_label.setScaledContents(False)
        self.scroll_area.setWidget(self.image_label)
        self.apply_background_color()
        self.filename_label = QLabel('')
        self.filename_label.setAlignment(Qt.AlignCenter)
        self.filename_label.setStyleSheet("color: white; background-color: rgba(0,0,0,0.7); padding: 5px;")
        self.filename_label.hide()
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.setMouseTracking(True)
        self.scroll_area.setMouseTracking(True)
        self.image_label.setMouseTracking(True)
        self.scroll_area.viewport().setMouseTracking(True)
        self.image_label.installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)
    
    def apply_background_color(self):
        bg_color = self.settings.get('background_color', '#2b2b2b')
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {bg_color}; }}
            QScrollArea {{ background-color: {bg_color}; }}
            QLabel {{ background-color: transparent; }}
        """)
    
    def get_resize_region(self, pos):
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        margin = self.resize_margin
        left = x < margin
        right = x > w - margin
        top = y < margin
        bottom = y > h - margin
        if left and top:
            return 'topleft'
        elif right and top:
            return 'topright'
        elif left and bottom:
            return 'bottomleft'
        elif right and bottom:
            return 'bottomright'
        elif left:
            return 'left'
        elif right:
            return 'right'
        elif top:
            return 'top'
        elif bottom:
            return 'bottom'
        else:
            return None
    
    def update_cursor(self, pos):
        if self.isFullScreen():
            # Resizing isn't possible in fullscreen, so never show a resize cursor there.
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            return
        region = self.get_resize_region(pos)
        if region in ['left', 'right']:
            self.setCursor(Qt.SizeHorCursor)
        elif region in ['top', 'bottom']:
            self.setCursor(Qt.SizeVerCursor)
        elif region in ['topleft', 'bottomright']:
            self.setCursor(Qt.SizeFDiagCursor)
        elif region in ['topright', 'bottomleft']:
            self.setCursor(Qt.SizeBDiagCursor)
        else:
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
    
    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            if self.isFullScreen():
                self.show_cursor()
                self.showNormal()
                self.reset_cursor_timer()
                event.accept()
                return
        
        key_sequence = QKeySequence(event.modifiers() | event.key()).toString()
        close_shortcuts = self.settings.get_shortcuts('close_program')
        if key_sequence in close_shortcuts:
            QTimer.singleShot(150, self.close)
            event.accept()
            return
        shortcut_actions = {
            'next_image': self.next_image, 'prev_image': self.prev_image,
            'zoom_in': self.zoom_in, 'zoom_out': self.zoom_out,
            'toggle_actual_size': self.toggle_actual_size,
            'toggle_fullscreen': self.toggle_fullscreen,
            'show_image_list': self.show_image_list_dialog,
            'delete_image': self.delete_image, 'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
        }
        for action_name, callback in shortcut_actions.items():
            shortcuts = self.settings.get_shortcuts(action_name)
            if key_sequence in shortcuts:
                callback()
                event.accept()
                return
        event.accept()
    
    def check_mouse_shortcut(self, button_text):
        actions = {
            'next_image': self.next_image, 'prev_image': self.prev_image,
            'toggle_fullscreen': self.toggle_fullscreen,
            'close_program': self.close_program,
            'show_image_list': self.show_image_list_dialog,
            'zoom_in': self.zoom_in, 'zoom_out': self.zoom_out,
            'toggle_actual_size': self.toggle_actual_size,
            'delete_image': self.delete_image, 'open_file': self.open_file,
            'slideshow': self.toggle_slideshow,
        }
        for action_name, callback in actions.items():
            shortcuts = self.settings.get_shortcuts(action_name)
            if button_text in shortcuts:
                callback()
                return True
        return False
    
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.load_path(urls[0].toLocalFile())
            event.acceptProposedAction()
    
    def load_settings(self):
        geometry = self.settings.get('window_geometry')
        if geometry and not self.isFullScreen():
            if isinstance(geometry, dict):
                x = geometry.get('x', 100)
                y = geometry.get('y', 100)
                w = geometry.get('width', 800)
                h = geometry.get('height', 600)
                screen = QApplication.primaryScreen().availableGeometry()
                if x < screen.left():
                    x = screen.left()
                if y < screen.top():
                    y = screen.top()
                if x + w > screen.right():
                    x = max(screen.left(), screen.right() - w)
                if y + h > screen.bottom():
                    y = max(screen.top(), screen.bottom() - h)
                self.setGeometry(x, y, w, h)
    
    def save_settings(self):
        if not self.isFullScreen():
            pos = self.pos()
            size = self.size()
            geometry_data = {
                'x': pos.x(),
                'y': pos.y(),
                'width': size.width(),
                'height': size.height()
            }
            self.settings.set('window_geometry', geometry_data)
        self.settings.set('zip_position_history', dict(self.zip_position_history))
    
    def load_path(self, path):
        self.bring_to_front()
        if os.path.isdir(path):
            self.load_directory(path)
        elif os.path.isfile(path):
            if ZipHandler.is_zip(path):
                self.load_zip(path)
            else:
                self.load_single_file(path)
    
    def natural_sort_key(self, s):
        return [int(text) if text.isdigit() else text.lower() 
                for text in re.split(r'(\d+)', s)]
    
    def load_directory(self, directory):
        self.load_generation += 1
        self.cache_manager.clear()
        self.image_list = []
        self.current_zip = None
        try:
            files = sorted(os.listdir(directory), key=self.natural_sort_key)
            for filename in files:
                if ImageLoader.is_supported(filename):
                    self.image_list.append(os.path.join(directory, filename))
        except:
            pass
        if self.image_list:
            self.current_index = 0
            self.show_current_image()
        else:
            self.image_label.clear()
    
    def load_single_file(self, filepath):
        directory = os.path.dirname(filepath)
        self.load_directory(directory)
        try:
            abs_path = os.path.abspath(filepath)
            for i, img_path in enumerate(self.image_list):
                if os.path.abspath(img_path) == abs_path:
                    self.current_index = i
                    break
            self.show_current_image()
        except:
            pass
    
    def load_zip(self, zip_path):
        self.load_generation += 1
        self.cache_manager.clear()
        self.current_zip = zip_path
        self.image_list = ZipHandler.list_images(zip_path)
        if self.image_list:
            self.current_index = 0
            if self.settings.get('remember_zip_position', False):
                remembered = self.zip_position_history.get(os.path.abspath(zip_path))
                if remembered in self.image_list:
                    self.current_index = self.image_list.index(remembered)
            self.show_current_image()
        else:
            self.image_label.clear()

    def _remember_zip_position(self, filename):
        """Tracks the last-viewed file for the current zip archive in
        zip_position_history (see load_zip for where this gets read back
        on the next open), capped at 30 archives -- move_to_end here and
        popitem(last=False) below make the cap an LRU one, evicting
        whichever archive was touched longest ago."""
        key = os.path.abspath(self.current_zip)
        self.zip_position_history[key] = filename
        self.zip_position_history.move_to_end(key)
        while len(self.zip_position_history) > 30:
            self.zip_position_history.popitem(last=False)
    
    def stop_current_movie(self):
        if self.current_movie:
            try:
                self.current_movie.frameChanged.disconnect(self.on_gif_frame_changed)
            except:
                pass
            try:
                self.current_movie.frameChanged.disconnect(self.on_animated_frame_changed)
            except:
                pass
            self.gif_frame_connected = False
            self.current_movie.stop()
            self.current_movie = None
        if self.current_movie_buffer is not None:
            try:
                self.current_movie_buffer.close()
            except:
                pass
            self.current_movie_buffer = None
        if self.prefetch_movie is not None:
            try:
                self.prefetch_movie.stop()
            except:
                pass
            self.prefetch_movie = None
        if self.prefetch_buffer is not None:
            try:
                self.prefetch_buffer.close()
            except:
                pass
            self.prefetch_buffer = None
        self.prefetch_frame_count = None
        self.animated_inflight_keys.clear()
        self.current_movie_original_size = None
        self.current_movie_target_size = None
        self.current_movie_generation += 1
        self.current_movie_frame = -1
        self.gif_last_frame = -1
        self.animated_frame_cache.clear()
    
    def _cache_key(self, filepath, saturation, brightness, contrast, max_size):
        size_key = 'full' if not max_size else f'{max_size[0]}x{max_size[1]}'
        source = f'{self.current_zip}|{filepath}' if self.current_zip else filepath
        return f'{source}|{saturation}|{brightness}|{contrast}|{size_key}'

    def _target_decode_size(self):
        if not self.fit_to_window:
            return None
        size = self.scroll_area.size()
        dpr = self.devicePixelRatioF()
        # Decode at physical-pixel resolution, not device-independent-pixel
        # resolution -- on a scaled display (e.g. Windows at 200%) the DIP
        # size is only half the physical detail the screen can actually
        # show, so fit-to-window images decoded at that size look softer
        # than they need to. The matching fix that actually keeps this
        # extra detail on screen instead of immediately downscaling it away
        # again is the scaled-to-physical-size + setDevicePixelRatio() call
        # in update_image_display's fit_to_window branch below.
        # Small safety margin (also scaled) prevents repeated reloads
        # caused by tiny widget changes.
        return (max(64, int((size.width() + 64) * dpr)), max(64, int((size.height() + 64) * dpr)))

    def _source_cache_get(self, key):
        value = self._source_image_cache.get(key)
        if value is not None:
            try:
                self._source_image_cache_order.remove(key)
            except ValueError:
                pass
            self._source_image_cache_order.append(key)
        return value

    def _source_cache_put(self, key, image):
        if image is None:
            return
        self._source_image_cache[key] = image
        try:
            self._source_image_cache_order.remove(key)
        except ValueError:
            pass
        self._source_image_cache_order.append(key)
        while len(self._source_image_cache_order) > self._source_image_cache_limit:
            old = self._source_image_cache_order.pop(0)
            self._source_image_cache.pop(old, None)

    def _adjusted_cache_key(self, filepath, saturation, brightness, contrast, max_size):
        source = f'{self.current_zip}|{filepath}' if self.current_zip else filepath
        # max_size is included so a color-adjusted image decoded for one
        # window/display size is never handed back as the result for a
        # request at a different size (e.g. right after a resize) -- see
        # _submit_image_load. It's already a plain (w, h) int tuple or
        # None from _target_decode_size(), so it's hashable as-is.
        return (source, int(saturation), int(brightness), int(contrast), max_size)

    def _adjusted_cache_get(self, key):
        value = self._adjusted_image_cache.get(key)
        if value is not None:
            try:
                self._adjusted_image_cache_order.remove(key)
            except ValueError:
                pass
            self._adjusted_image_cache_order.append(key)
        return value

    def _adjusted_cache_put(self, key, image):
        if image is None:
            return
        self._adjusted_image_cache[key] = image
        try:
            self._adjusted_image_cache_order.remove(key)
        except ValueError:
            pass
        self._adjusted_image_cache_order.append(key)
        while len(self._adjusted_image_cache_order) > self._adjusted_image_cache_limit:
            old = self._adjusted_image_cache_order.pop(0)
            self._adjusted_image_cache.pop(old, None)

    def _submit_image_load(self, index, generation=None, force=False):
        if ImageLoader._shutdown:
            ImageLoader.restart_executor()
        if generation is None:
            generation = self.load_generation
        if index < 0 or index >= len(self.image_list):
            return
        filename = self.image_list[index]
        saturation = self.settings.get('saturation', 100)
        brightness = self.settings.get('brightness', 100)
        contrast = self.settings.get('contrast', 100)
        max_size = self._target_decode_size()
        key = self._cache_key(filename, saturation, brightness, contrast, max_size)
        if not force and self.cache_manager.get(key) is not None:
            return
        if key in self.loading_keys:
            return
        self.loading_keys.add(key)
        source_zip = self.current_zip
        def worker():
            source_fast_key = ((source_zip, filename), max_size)
            if (saturation, brightness, contrast) == (100, 100, 100):
                source_cached = self._source_cache_get(source_fast_key)
                if source_cached is not None:
                    self.load_bridge.loaded.emit(
                        generation, key, source_cached, index == self.current_index
                    )
                    return

            adjustment_key = self._adjusted_cache_key(filename, saturation, brightness, contrast, max_size)
            # For non-default adjustments, reuse the expensive color-adjusted
            # source when available. The existing display cache still handles
            # the fit-to-window/full-size distinction.
            if (saturation, brightness, contrast) != (100, 100, 100):
                cached_adjusted = self._adjusted_cache_get(adjustment_key)
                if cached_adjusted is not None:
                    image = cached_adjusted
                    self.load_bridge.loaded.emit(generation, key, image, index == self.current_index)
                    return

            if source_zip:
                image = ZipHandler.load_image_data(source_zip, filename, saturation, brightness, contrast, max_size)
            else:
                image = ImageLoader.load_image_data(filename, saturation, brightness, contrast, max_size)
                if image is None and max_size is None:
                    try:
                        image = QImage(filename)
                    except Exception:
                        image = None

            if image is not None and (saturation, brightness, contrast) != (100, 100, 100):
                self._adjusted_cache_put(adjustment_key, image)
            self.load_bridge.loaded.emit(generation, key, image, index == self.current_index)
        try:
            ImageLoader._executor.submit(worker)
        except Exception as e:
            self.loading_keys.discard(key)
            print(f"백그라운드 로딩 시작 오류: {e}")

    def _on_background_loaded(self, generation, key, image, was_current):
        # A preload may have been started for an older navigation generation.
        # Its result is still valuable: keep it in cache. Only the paint decision
        # must be based on the image that is current *now*.
        self.loading_keys.discard(key)

        current_key = None
        if self.image_list and 0 <= self.current_index < len(self.image_list):
            current_key = self._cache_key(
                self.image_list[self.current_index],
                self.settings.get('saturation', 100),
                self.settings.get('brightness', 100),
                self.settings.get('contrast', 100),
                self._target_decode_size()
            )

        if image is None or image.isNull():
            if key == current_key:
                self._retry_or_fail_current_load(key)
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            if key == current_key:
                self._retry_or_fail_current_load(key)
            return

        self.load_retry_counts.pop(key, None)
        self.cache_manager.put(key, pixmap)

        # Display only if this result matches what is visible right now.
        # This fixes rapid navigation races where an older worker finishes late.
        if key == current_key:
            self._display_pixmap(pixmap)
            self.is_loading = False
            self.slideshow_fail_streak = 0

    def _retry_or_fail_current_load(self, key):
        # Never blank the viewer on the first failure -- retry the currently
        # requested image once, after a short delay, in case it was just a
        # transient read hiccup (e.g. a locked/still-being-written file).
        count = self.load_retry_counts.get(key, 0)
        if count < 1:
            self.load_retry_counts[key] = count + 1
            QTimer.singleShot(
                40,
                lambda k=key, g=self.load_generation:
                    self._retry_current_load(k, g)
            )
        else:
            self.load_retry_counts.pop(key, None)
            self._handle_unreadable_current_image()

    def _handle_unreadable_current_image(self):
        # The retry above was also exhausted: the current file has a
        # supported extension but its data genuinely can't be decoded
        # (corrupted/truncated, etc). During a slideshow this must not just
        # sit there waiting for a signal that will never come (this is what
        # used to freeze a GIF-loop-mode slideshow, since a movie that never
        # started never emits the frameChanged it needs to count loops) --
        # skip past it automatically instead. While browsing manually,
        # replace the stale previous frame with an explicit "broken image"
        # placeholder rather than leaving old content on screen that looks
        # like it belongs to this file.
        self.is_loading = False
        if self.slideshow_playing:
            self.slideshow_fail_streak += 1
            if self.slideshow_fail_streak > min(len(self.image_list), 200):
                # Every remaining image is failing to load; stop instead of
                # spinning through the whole list indefinitely.
                self.stop_slideshow()
                self._display_pixmap(self._default_broken_pixmap())
                return
            self.next_image()
        else:
            self._display_pixmap(self._default_broken_pixmap())

    def _default_broken_pixmap(self):
        # Now the only broken-file placeholder (the custom-file picker was
        # removed from Settings) -- the user's own illustration, embedded
        # as base64 (_BROKEN_IMAGE_B64 near the top of the file) so there's
        # no external image file a packaged build could end up missing.
        # Falls back to a plain drawn placeholder if decoding ever fails
        # (a corrupted constant, an unsupported Qt build, etc.) rather than
        # showing nothing at all for a broken file.
        if self._default_broken_pixmap_cache is None:
            pixmap = QPixmap()
            try:
                data = base64.b64decode(_BROKEN_IMAGE_B64)
                if not pixmap.loadFromData(data, 'PNG') or pixmap.isNull():
                    pixmap = None
            except Exception:
                pixmap = None
            if pixmap is None:
                pixmap = self._draw_fallback_broken_pixmap()
            self._default_broken_pixmap_cache = pixmap
        return self._default_broken_pixmap_cache

    def _draw_fallback_broken_pixmap(self):
        # Only reached if decoding the embedded image above ever fails.
        # Drawn entirely in code (no file of its own to go missing either)
        # -- a simple picture-frame + mountain/sun glyph (the familiar
        # "broken image" shape browsers use) with a crack through it to
        # read as broken rather than just "a picture".
        pixmap = QPixmap(400, 300)
        pixmap.fill(QColor('#3c3c3c'))
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            frame = pixmap.rect().adjusted(130, 70, -130, -110)
            painter.setPen(QPen(QColor('#888888'), 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(frame, 6, 6)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#888888'))
            sun_d = 16
            painter.drawEllipse(frame.right() - 28, frame.top() + 14, sun_d, sun_d)
            mountains = QPolygon([
                QPoint(frame.left() + 10, frame.bottom() - 10),
                QPoint(frame.left() + 40, frame.bottom() - 45),
                QPoint(frame.left() + 60, frame.bottom() - 25),
                QPoint(frame.left() + 85, frame.bottom() - 55),
                QPoint(frame.right() - 10, frame.bottom() - 10),
            ])
            painter.drawPolygon(mountains)
            painter.setPen(QPen(QColor('#e06060'), 4))
            painter.drawLine(frame.left() - 6, frame.top() - 6, frame.right() + 6, frame.bottom() + 6)
            painter.setPen(QColor('#cccccc'))
            font = painter.font()
            font.setPointSize(13)
            painter.setFont(font)
            text_rect = pixmap.rect().adjusted(0, frame.bottom() + 20, 0, 0)
            painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, '이미지를 불러올 수 없습니다')
        finally:
            painter.end()
        return pixmap

    def _retry_current_load(self, key, generation):
        if generation != self.load_generation:
            return
        if not self.image_list or not (0 <= self.current_index < len(self.image_list)):
            return
        current_key = self._cache_key(
            self.image_list[self.current_index],
            self.settings.get('saturation', 100),
            self.settings.get('brightness', 100),
            self.settings.get('contrast', 100),
            self._target_decode_size()
        )
        if key == current_key and self.cache_manager.get(key) is None:
            self._submit_image_load(self.current_index, generation, force=True)
        
    def _display_pixmap(self, pixmap):
        if not pixmap or pixmap.isNull():
            return
        self.current_pixmap = pixmap
        self.original_pixmap = pixmap
        self.update_image_display()
        if self.settings.get('show_filename', False):
            current_file = self.image_list[self.current_index]
            display_name = os.path.basename(current_file) if not self.current_zip else current_file
            self.filename_label.setText(display_name)
            self.filename_label.show()
            self.filename_label.adjustSize()
            self.filename_label.move(10, 10)
        else:
            self.filename_label.hide()

    def _preload_neighbors(self):
        if not self.preload_enabled or not self.image_list:
            return
        count = max(0, min(10, int(self.preload_count)))
        if count <= 0:
            return

        # Preload symmetrically around the current image. Nearer images are
        # submitted first so the immediately-next image gets priority.
        generation = self.load_generation
        for distance in range(1, count + 1):
            for direction in (1, -1):
                idx = self.current_index + (distance * direction)
                if 0 <= idx < len(self.image_list):
                    self._submit_image_load(idx, generation)

    def _set_display_pixmap(self, pixmap):
        if pixmap is None or pixmap.isNull():
            return False
        self.image_label.setPixmap(pixmap)
        return True

    def _load_animated_movie(self, current_file, ext):
        """Try to load current_file as a playable QMovie (an animated gif,
        or a webp with more than one frame). Works whether the file sits on
        disk or inside the currently open zip -- QMovie can't read a zip
        path directly, so a zip entry is read into memory first and handed
        to QMovie through a QBuffer.

        Returns (movie, buffer, frame_count):
          - movie is None both when the file isn't an animated gif/webp and
            when it is one but couldn't actually be decoded (corrupted/
            truncated data). Either way the caller should fall back to the
            regular static-image path, which is also what surfaces a
            genuine decode failure through the normal load-failure handling
            (retry, then slideshow auto-skip / broken-image placeholder).
          - buffer is the QBuffer backing a zip-sourced movie. It must be
            kept alive (self.current_movie_buffer) for as long as the movie
            is in use -- QMovie keeps reading frames from it as playback
            advances, it doesn't copy the data up front. It's None for a
            movie loaded straight from a real file path.
          - frame_count is the animation's real frame count, for both gif
            and webp (Pillow already had to report it via get_frame_count
            below to confirm the file is actually animated, so it's free
            here -- movie.frameCount() is unreliable for many animated
            webp files).
          - source is the raw bytes read from the zip entry (or None when
            the file was read straight from current_file on disk). Callers
            that want a second, independent QMovie on the same data (e.g.
            for frame look-ahead) can pass this straight back into
            _build_qmovie() -- re-reading the zip entry a second time is
            avoided since the bytes are already in memory here.
        """
        if ext not in ('.gif', '.webp'):
            return None, None, None, None

        data = None
        if self.current_zip:
            try:
                zf = ZipHandler._get_zip(self.current_zip)
                with zf.open(current_file, 'r') as fp:
                    data = fp.read()
            except Exception:
                return None, None, None, None

        # get_frame_count tells a static (single-frame) gif/webp apart from
        # a genuinely animated one; a static one belongs on the normal
        # image path instead of QMovie. This used to only be checked for
        # webp -- every .gif went straight to QMovie regardless of frame
        # count -- which is what let a single-frame gif get stuck not
        # resizing with the window (see get_frame_count's docstring).
        frame_count = get_frame_count(BytesIO(data) if data is not None else current_file)
        if not frame_count:
            return None, None, None, None

        movie, buffer = self._build_qmovie(current_file, data)
        if movie is None:
            return None, None, None, None

        return movie, buffer, frame_count, data

    def _build_qmovie(self, current_file, data):
        """Build one playable QMovie from either raw bytes (data, for a zip
        entry) or a path on disk (current_file, when data is None). Used
        both for the live/display movie and for the independent look-ahead
        movie in show_current_image, so the two always decode identically."""
        buffer = None
        movie = None
        try:
            if data is not None:
                buffer = QBuffer()
                buffer.setData(QByteArray(data))
                if not buffer.open(QIODevice.ReadOnly):
                    return None, None
                movie = QMovie()
                movie.setDevice(buffer)
            else:
                movie = QMovie(current_file)
            if not movie.isValid():
                raise ValueError('invalid movie')
            movie.jumpToFrame(0)
            first_frame = movie.currentPixmap()
            if first_frame.isNull() or first_frame.width() <= 0:
                raise ValueError('first frame failed to decode')
        except Exception:
            if buffer is not None:
                try:
                    buffer.close()
                except Exception:
                    pass
            return None, None

        return movie, buffer

    def show_current_image(self):
        if not self.image_list or self.current_index < 0 or self.current_index >= len(self.image_list):
            return
        self.load_generation += 1
        generation = self.load_generation
        self.stop_current_movie()
        current_file = self.image_list[self.current_index]
        if self.current_zip and self.settings.get('remember_zip_position', False):
            self._remember_zip_position(current_file)
        saturation = self.settings.get('saturation', 100)
        brightness = self.settings.get('brightness', 100)
        contrast = self.settings.get('contrast', 100)

        # Animated GIF/WebP: keep QMovie for timing/decoding, and render each
        # frame through an in-memory filter on demand as it plays (only the
        # frame currently on screen is ever processed, cached by frame
        # number + anim_* settings so repeat loops are free). This works the
        # same way for a file inside a zip as for one on disk --
        # _load_animated_movie reads the zip entry into memory and hands
        # QMovie a QBuffer instead of a file path. Moving images use their
        # own anim_* saturation/brightness/contrast settings, independent
        # from the ones used for static images (see _render_animated_frame).
        ext = os.path.splitext(current_file)[1].lower()
        movie, movie_buffer, known_frame_count, movie_source = self._load_animated_movie(current_file, ext)
        if movie:
            self.current_movie = movie
            self.current_movie_buffer = movie_buffer
            self.current_movie_generation += 1
            movie_generation = self.current_movie_generation
            self.current_movie_original_size = movie.currentPixmap().size()
            self.scroll_area.setWidgetResizable(self.fit_to_window)

            # Built before the scaled-size is applied below so
            # _apply_anim_scaled_size can sync both movies in one place.
            self.prefetch_movie, self.prefetch_buffer = self._build_qmovie(current_file, movie_source)

            scaled_size = None
            if self.current_movie_original_size.width() > 0:
                if self.fit_to_window:
                    # Same physical-pixel-target fix as
                    # update_image_display's fit_to_window branch -- this
                    # is a separate computation (for the very first frame,
                    # before any resize/zoom event has happened) that was
                    # missed when that one was fixed, which is why the
                    # correct size only ever showed up *after* a window
                    # resize forced update_image_display to run: switching
                    # to a new animated image, or pressing "actual size" to
                    # re-enter fit-to-window, kept landing here instead and
                    # rendering at half the intended resolution on a
                    # scaled display.
                    dpr = self.devicePixelRatioF()
                    scaled_size = self.current_movie_original_size.scaled(self.scroll_area.size() * dpr, Qt.KeepAspectRatio)
                else:
                    scaled_size = QSize(int(self.current_movie_original_size.width() * self.zoom_factor),
                                         int(self.current_movie_original_size.height() * self.zoom_factor))
                if scaled_size.width() > 0 and scaled_size.height() > 0:
                    self._apply_anim_scaled_size(scaled_size)
                else:
                    scaled_size = None
            self.current_movie_frame = -1
            self.animated_frame_cache.clear()
            self.slideshow_fail_streak = 0

            # Size the frame cache to fit the animation's whole loop
            # (bounded by a memory budget, since frames can be large),
            # instead of a flat 24-frame limit. The frame count is already
            # known for free here -- get_frame_count (via Pillow) already
            # had to determine it for both gif and webp just to confirm
            # the file is genuinely animated before reaching this point,
            # so using it here costs no extra decoding.
            # A too-small fixed cache is what let a long/heavy animation's
            # frames get evicted before their next loop could reuse them,
            # so color processing (and the staleness that comes with it)
            # never actually settled down no matter how long you waited.
            frame_count = known_frame_count or movie.frameCount()
            if frame_count and frame_count > 0:
                w = scaled_size.width() if scaled_size else self.current_movie_original_size.width()
                h = scaled_size.height() if scaled_size else self.current_movie_original_size.height()
                if w > 0 and h > 0:
                    bytes_per_frame = w * h * 4
                    budget = 800 * 1024 * 1024  # ~800MB ceiling for this cache
                    by_memory = max(1, budget // max(1, bytes_per_frame))
                    self.animated_frame_cache_limit = max(24, min(frame_count, by_memory, 600))
                else:
                    self.animated_frame_cache_limit = max(24, min(frame_count, 300))
            else:
                self.animated_frame_cache_limit = 24

            self.prefetch_frame_count = frame_count if frame_count and frame_count > 0 else None

            # A new movie must reconnect slideshow loop counting.
            if self.slideshow_playing and self.slideshow_mode == 'loop':
                self.connect_gif_loop()
            movie.frameChanged.connect(self.on_animated_frame_changed)
            movie.start()
            start_frame = movie.currentFrameNumber()
            self._render_animated_frame(start_frame, movie_generation)
            self._prefetch_ahead(start_frame)
            self.is_loading = False
            return

        max_size = self._target_decode_size()
        key = self._cache_key(current_file, saturation, brightness, contrast, max_size)
        cached = self.cache_manager.get(key)
        if cached is not None:
            self.is_loading = False
            self._display_pixmap(cached)
            self._preload_neighbors()
            return

        self.is_loading = True
        # Keep the previous frame visible while the new image is decoding.
        # Clearing the label here caused the frequent black-screen effect during
        # rapid navigation. A successful decode will replace it atomically.
        self._submit_image_load(self.current_index, generation)
        self._preload_neighbors()

    def _animated_cache_key(self, frame_number):
        return (frame_number,
                self.settings.get('anim_saturation', 100),
                self.settings.get('anim_brightness', 100),
                self.settings.get('anim_contrast', 100),
                self.fit_to_window,
                self.zoom_factor,
                self.scroll_area.size().width(),
                self.scroll_area.size().height())

    def _anim_decode_size(self, target_size):
        """The size QMovie should actually decode/scale a frame to. Capped
        at the animation's native resolution: when the user is zoomed in
        past 1:1, letting QMovie upscale every frame before our own color
        filter runs on it means the filter -- and every raw-bytes copy
        around it in _submit_animated_frame_processing -- pays for pixels
        that carry no extra information over the native frame. Instead the
        movie decodes at native resolution and the upscale to the actual
        on-screen size happens once, cheaply (a single resize), after the
        per-pixel color math instead of before it. When target_size is at
        or below native resolution (fit-to-window, or zoomed out) this is a
        no-op -- that case was already cheap and correct."""
        orig = self.current_movie_original_size
        if not target_size or not orig or orig.width() <= 0 or orig.height() <= 0:
            return target_size
        if target_size.width() <= orig.width() and target_size.height() <= orig.height():
            return target_size
        return orig

    def _apply_anim_scaled_size(self, target_size):
        """Set the on-screen target size for the current animation and sync
        both the live and look-ahead movies to the (possibly smaller,
        native-capped) decode size. Call this -- not setScaledSize()
        directly -- on load and on every zoom/window-size change, so the
        two movies never drift apart; see the note in _prefetch_ahead about
        what happens when they do."""
        self.current_movie_target_size = target_size
        decode_size = self._anim_decode_size(target_size)
        if self.current_movie:
            self.current_movie.setScaledSize(decode_size)
        if self.prefetch_movie:
            self.prefetch_movie.setScaledSize(decode_size)

    def on_animated_frame_changed(self, frame_number):
        if not self.current_movie:
            return
        self.current_movie_frame = frame_number
        self._render_animated_frame(frame_number, self.current_movie_generation)
        # Keep the next couple of frames a step ahead of playback so their
        # color processing is already sitting in cache by the time the
        # movie actually reaches them, instead of starting cold each time.
        self._prefetch_ahead(frame_number)

    def _render_animated_frame(self, frame_number, generation):
        if not self.current_movie or generation != self.current_movie_generation:
            return
        movie = self.current_movie
        qimage = movie.currentImage()
        if qimage.isNull():
            qimage = movie.currentPixmap().toImage()
        if qimage.isNull():
            return
        key = self._animated_cache_key(frame_number)
        cached = self.animated_frame_cache.get(key)
        if cached is not None:
            self.animated_frame_cache.move_to_end(key)
            self.current_pixmap = cached
            self.image_label.setPixmap(cached)
            self.image_label.adjustSize()
            return

        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)
        if saturation == 100 and brightness == 100 and contrast == 100:
            pixmap = QPixmap.fromImage(qimage)
            target = self.current_movie_target_size
            if target and (pixmap.width() != target.width() or pixmap.height() != target.height()):
                mode = Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
                pixmap = pixmap.scaled(target, Qt.KeepAspectRatio, mode)
            self._store_animated_frame(key, pixmap)
            self.current_pixmap = pixmap
            self.image_label.setPixmap(pixmap)
            self.image_label.adjustSize()
            return

        # Try the GPU shader tier first -- it runs synchronously right
        # here (fast enough not to need the anim worker pool) and, on
        # success, skips both the cv2 and Pillow tiers below entirely.
        # Only on GPU failure (unsupported driver, first-time init error,
        # etc.) does this fall through to _submit_animated_frame_processing,
        # whose own worker() tries cv2 before Pillow.
        pixmap = self._render_animated_frame_gpu(qimage, saturation, brightness, contrast)
        if pixmap is not None:
            self._store_animated_frame(key, pixmap)
            self.current_pixmap = pixmap
            self.image_label.setPixmap(pixmap)
            self.image_label.adjustSize()
            return

        # A look-ahead prefetch may already be processing this exact frame;
        # if so just wait for that result instead of computing it twice.
        if key in self.animated_inflight_keys:
            return
        self._submit_animated_frame_processing(qimage, frame_number, generation, key)

    def _render_animated_frame_gpu(self, qimage, saturation, brightness, contrast):
        """GPU-shader replacement for the apply_color_adjustments()/
        apply_color_adjustments_cv2() call in _submit_animated_frame_
        processing, for the frame that's actually about to be displayed.
        Renders synchronously (see GpuColorCorrector.adjust) and returns
        a ready-to-display QPixmap already sized to
        current_movie_target_size, or None if the GPU path isn't
        available -- callers fall back to the unchanged cv2/Pillow tiers
        in that case."""
        target = self.current_movie_target_size
        target_w = target.width() if target else qimage.width()
        target_h = target.height() if target else qimage.height()
        # See _ANIM_GPU_MAX_PIXELS: past this size, the synchronous
        # upload+render+readback below is itself slow enough to stall the
        # GUI thread for a visible moment, which is exactly the "large
        # animated webp playback is slow" complaint -- the async cv2/
        # Pillow tiers the caller falls back to don't have that problem,
        # since they run off the GUI thread.
        if target_w * target_h > _ANIM_GPU_MAX_PIXELS:
            return None
        result = self.gl_color_corrector.adjust(
            qimage, saturation / 100.0, brightness / 100.0, contrast / 100.0,
            target_w, target_h)
        if result is None or result.isNull():
            return None
        return QPixmap.fromImage(result)

    def _submit_animated_frame_processing(self, qimage, frame_number, generation, key):
        # Process the frame in the anim worker pool (kept separate from the
        # static-image pool -- see _ANIM_WORKER_COUNT). QMovie itself stays
        # on the GUI thread because it's a Qt object; the expensive color
        # work happens off the UI thread and never creates a temp file.
        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)
        target = self.current_movie_target_size
        target_w = target.width() if target else None
        target_h = target.height() if target else None
        try:
            rgba = qimage.convertToFormat(QImage.Format_RGBA8888)
            w, h = rgba.width(), rgba.height()
            ptr = rgba.bits()
            ptr.setsize(rgba.byteCount())
            raw = bytes(ptr)
            self.animated_inflight_keys.add(key)
            def worker():
                # cv2 path first (see _process_animated_frame_fast) --
                # roughly an order of magnitude faster than the PIL path
                # below for this. Falls through to PIL on any failure,
                # most commonly because opencv-python-headless just isn't
                # installed, so playback still works either way.
                result = _process_animated_frame_fast(raw, w, h, saturation, brightness, contrast, target_w, target_h)
                if result is not None:
                    return result
                try:
                    from PIL import Image
                    src = Image.frombuffer('RGBA', (w, h), raw, 'raw', 'RGBA', 0, 1)
                    rgb = src.convert('RGB')
                    # Color math runs at native (w, h) resolution -- see
                    # _anim_decode_size -- so the upscale to the on-screen
                    # target size, if any, happens once here at the end
                    # instead of the filter paying for the extra pixels.
                    rgb = apply_color_adjustments(rgb, saturation, brightness, contrast)
                    if target_w and target_h and (rgb.width != target_w or rgb.height != target_h):
                        resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                        rgb = rgb.resize((target_w, target_h), resample)
                    out = rgb.convert('RGBA')
                    return out.tobytes('raw', 'RGBA'), rgb.width, rgb.height
                except Exception:
                    return None
            future = ImageLoader._anim_executor.submit(worker)
            def done(fut, gen=generation, frame=frame_number, key=key):
                try:
                    result = fut.result()
                except Exception:
                    result = None
                self.load_bridge.animated_frame.emit(gen, frame, (key, result))
            future.add_done_callback(done)
        except Exception:
            self.animated_inflight_keys.discard(key)

    def _prefetch_ahead(self, frame_number):
        """Decode the next anim_lookahead frames on the paused prefetch
        movie and dispatch them to the cv2/Pillow worker pool now (see
        _submit_animated_frame_processing), so they're ready in
        animated_frame_cache before playback actually reaches them.

        Deliberately never uses the synchronous GPU tier
        (_render_animated_frame_gpu) here, even though
        _render_animated_frame does for the frame that's actually about
        to be displayed: prefetching exists precisely because these
        frames *aren't* needed yet, so there's no reason to pay a
        synchronous GUI-thread cost for them. That used to cost up to two
        extra synchronous texture-upload+FBO-render+readback round trips
        on the GUI thread per frame change -- worse for playback
        smoothness the larger the frame, and worse than just letting this
        work happen off-thread, which is the whole point of prefetching."""
        if not self.prefetch_movie or not self.current_movie:
            return
        # No adjustment active: the live path takes a free instant fast path
        # (QPixmap.fromImage with no Pillow round-trip), so there's nothing
        # worth precomputing here.
        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)
        if saturation == 100 and brightness == 100 and contrast == 100:
            return
        total = self.prefetch_frame_count or self.current_movie.frameCount()
        if not total or total <= 1:
            return
        # Backpressure: every prefetch frame goes through the cv2/Pillow
        # worker pool (see docstring above), so this caps how much
        # speculative work rides along on top of it. If the pool is
        # already as busy as it can usefully be (e.g. a large/zoomed
        # frame is taking a while), don't pile more onto it -- that only
        # pushes the frame that's actually about to be displayed further
        # back in the queue, which is what made zoomed-in playback feel
        # *slower* than before prefetching existed. Just skip this round;
        # the next real frame change will try again once things free up.
        if len(self.animated_inflight_keys) >= _ANIM_WORKER_COUNT:
            return
        generation = self.current_movie_generation
        for step in range(1, self.anim_lookahead + 1):
            target = (frame_number + step) % total
            key = self._animated_cache_key(target)
            if key in self.animated_frame_cache or key in self.animated_inflight_keys:
                continue
            if len(self.animated_inflight_keys) >= _ANIM_WORKER_COUNT:
                break
            try:
                if not self.prefetch_movie.jumpToFrame(target):
                    continue
                qimage = self.prefetch_movie.currentImage()
                if qimage.isNull():
                    qimage = self.prefetch_movie.currentPixmap().toImage()
                if qimage.isNull():
                    continue
            except Exception:
                continue
            self._submit_animated_frame_processing(qimage, target, generation, key)

    def _on_animated_frame_ready(self, generation, frame_number, payload):
        key, result = payload
        self.animated_inflight_keys.discard(key)
        if generation != self.current_movie_generation or not self.current_movie:
            return
        if not result:
            return
        raw, w, h = result
        qimg = QImage(raw, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pixmap = QPixmap.fromImage(qimg)
        if pixmap.isNull():
            return
        self._store_animated_frame(key, pixmap)
        if self.current_movie_frame == frame_number:
            self.current_pixmap = pixmap
            self.image_label.setPixmap(pixmap)
            self.image_label.adjustSize()

    def _store_animated_frame(self, key, pixmap):
        # Every animated frame is now rendered at physical-pixel
        # resolution before reaching here -- update_image_display's
        # fit_to_window branch targets scroll_area.size() * dpr (for
        # sharpness on a scaled display), and the non-fit-to-window branch
        # targets native_size * zoom_factor, which is a physical-pixel
        # quantity by construction. An untagged QPixmap is laid out in
        # device-independent pixels, so either way it would show at
        # devicePixelRatioF()x its intended size without this. This is the
        # single place every animated frame passes through before being
        # cached/displayed (the no-adjustment fast path, the GPU tier, the
        # cv2/Pillow tier, and prefetch all call this), so tagging it here
        # fixes all of them at once.
        pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        self.animated_frame_cache[key] = pixmap
        self.animated_frame_cache.move_to_end(key)
        while len(self.animated_frame_cache) > self.animated_frame_cache_limit:
            self.animated_frame_cache.popitem(last=False)

    def connect_gif_loop(self):
        if self.current_movie and not self.gif_frame_connected:
            self.current_movie.frameChanged.connect(self.on_gif_frame_changed)
            self.gif_frame_connected = True
    
    def on_gif_frame_changed(self, frame_number):
        if not self.slideshow_playing or self.slideshow_mode != 'loop':
            self.gif_last_frame = frame_number
            return
        # Count a completed cycle only when the movie actually wraps from a
        # later frame back to frame 0. This avoids counting the initial frame
        # as a completed playback.
        if frame_number == 0 and self.gif_last_frame > 0:
            self.gif_loop_count += 1
            if self.gif_loop_count >= self.gif_max_loops:
                self.gif_loop_count = 0
                self.gif_last_frame = -1
                self.next_image()
                return
        self.gif_last_frame = frame_number
    
    def update_image_display(self):
        # A new scaled pixmap invalidates the old pan position. Qt will clamp
        # scrollbars to the new image bounds after the label is resized.
        if self.panning:
            self._end_image_pan()
        # QScrollArea only lets image_label take on its own (pixmap) size when
        # widgetResizable is False. With it True, Qt force-fits the label to
        # the viewport on every layout pass regardless of adjustSize() below,
        # so a zoomed image can never register as "larger than the viewport"
        # and panning/scrollbars never actually engage. Keep it True only for
        # the fit-to-window case, where auto-fitting is what we want anyway.
        self.scroll_area.setWidgetResizable(self.fit_to_window)
        if self.current_movie:
            try:
                if self.current_movie_original_size and self.current_movie_original_size.width() > 0:
                    original_size = self.current_movie_original_size
                else:
                    original_size = self.current_movie.currentPixmap().size()
                    if original_size.width() > 0:
                        self.current_movie_original_size = original_size
                if original_size.width() > 0 and original_size.height() > 0:
                    if self.fit_to_window:
                        # Physical-pixel target, same reasoning as
                        # _target_decode_size for the static-image path --
                        # scaling to the bare DIP viewport size here would
                        # throw away half the detail on a 200%-scaled
                        # display before it ever reached the screen.
                        # _store_animated_frame's setDevicePixelRatio()
                        # call is what keeps this rendering at the correct
                        # on-screen size despite the larger pixel count.
                        dpr = self.devicePixelRatioF()
                        scaled_size = original_size.scaled(self.scroll_area.size() * dpr, Qt.KeepAspectRatio)
                    else:
                        scaled_size = QSize(int(original_size.width() * self.zoom_factor),
                                           int(original_size.height() * self.zoom_factor))
                    if scaled_size.width() > 0 and scaled_size.height() > 0:
                        # _apply_anim_scaled_size caps the movies' own
                        # decode size at the native resolution and keeps
                        # the live and look-ahead movie in sync -- see its
                        # docstring for why that matters here.
                        self._apply_anim_scaled_size(scaled_size)
                    self.current_movie_frame = self.current_movie.currentFrameNumber()
                    self._render_animated_frame(self.current_movie_frame, self.current_movie_generation)
            except:
                pass
            return
        
        if self.current_pixmap:
            if self.fit_to_window:
                dpr = self.devicePixelRatioF()
                scaled = self.current_pixmap.scaled(
                    self.scroll_area.size() * dpr,
                    Qt.KeepAspectRatio,
                    Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
                )
                # current_pixmap is now decoded at physical resolution too
                # (see _target_decode_size), so without this the extra
                # detail decoded above would just get thrown away again
                # right here -- same reasoning as the zoom branch below and
                # _store_animated_frame's version of this for animated
                # frames.
                scaled.setDevicePixelRatio(dpr)
                self.image_label.setPixmap(scaled)
            else:
                new_size = self.current_pixmap.size() * self.zoom_factor
                scaled = self.current_pixmap.scaled(
                    new_size,
                    Qt.KeepAspectRatio,
                    Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
                )
                # See _store_animated_frame for the animated-frame side of
                # this same fix. Actual size (fit_to_window == False) means
                # one image pixel per physical screen pixel; an untagged
                # QPixmap is laid out in device-independent pixels, so on a
                # scaled display (e.g. Windows at 200%) it would otherwise
                # show at devicePixelRatioF()x its intended size.
                scaled.setDevicePixelRatio(self.devicePixelRatioF())
                self.image_label.setPixmap(scaled)
            self.image_label.adjustSize()

    def _apply_high_quality_resample(self):
        """Re-renders the currently displayed fit-to-window image with a
        sharper Lanczos resample, replacing the quick
        Qt.SmoothTransformation result update_image_display used moments
        ago. Only fires once window-resize activity has been quiet for a
        bit (see resizeEvent/toggle_actual_size) -- Lanczos (via Pillow)
        is noticeably sharper than Qt's built-in smooth scaling for a
        significant downscale like fit-to-window often needs, but
        measured at up to ~1 second for a large (24MP-ish) photo, which
        is far too slow to run on the GUI thread synchronously (that
        would freeze the window for up to a second right as resizing
        stops) or to redo on every single resize event during an active
        drag. So this dispatches to the shared decode pool and applies
        the result asynchronously instead -- see _on_hq_resample_ready.
        Static images only (an animated frame changes every fraction of a
        second regardless, so there's no "settled" moment for this to
        wait for)."""
        if not self.fit_to_window or self.current_movie or not self.current_pixmap:
            return
        if self._hq_resample_inflight:
            # A previous pass is still working through a large image; let
            # it finish rather than piling more Lanczos work onto the
            # shared decode pool. Later resize events keep re-arming this
            # timer, so a fresh pass still happens once things quiet down
            # again and the pool is free.
            return
        dpr = self.devicePixelRatioF()
        target = self.scroll_area.size() * dpr
        target_w, target_h = target.width(), target.height()
        if target_w <= 0 or target_h <= 0:
            return
        # current_pixmap only ever holds as much resolution as
        # _target_decode_size() asked for at the window size that was
        # current when it was decoded (see _submit_image_load). If the
        # window has since grown past that -- most obviously right after
        # launch, when double-clicking the file opens a small/default
        # window and the window is then immediately maximized/fullscreened
        # -- current_pixmap is now smaller than target_w x target_h.
        # PIL's thumbnail() below never upscales an image past its current
        # size, so resampling it here wouldn't sharpen anything -- it would
        # just re-clamp the image back down to that old, smaller size,
        # visibly shrinking it right after update_image_display's quick
        # Qt-upscale had already shown it correctly large (the
        # grows-then-snaps-back-to-actual-size bug). When the source is too
        # small like this, ask the loader for a proper re-decode at the
        # new, larger target size instead of resampling what we already
        # have -- show_current_image() serves it from cache if that size
        # was already decoded, or decodes it in the background and shows
        # it via the normal _on_background_loaded -> _display_pixmap path
        # once ready, same as navigating to a new image does.
        if self.current_pixmap.width() < target_w - 2 or self.current_pixmap.height() < target_h - 2:
            self.show_current_image()
            return
        try:
            rgba = self.current_pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
            w, h = rgba.width(), rgba.height()
            if w <= 0 or h <= 0:
                return
            ptr = rgba.bits()
            ptr.setsize(rgba.byteCount())
            raw = bytes(ptr)
        except Exception:
            return
        generation = self.load_generation
        self._hq_resample_inflight = True
        def worker():
            try:
                Image = get_pil_image()
                # Stay in RGBA the whole way through -- converting to RGB
                # here (an earlier version of this did) drops the alpha
                # channel entirely, and Image.convert('RGB') doesn't
                # composite transparent pixels onto anything first, it
                # just keeps whatever RGB values happened to be stored
                # under the now-discarded alpha -- which for a PNG with a
                # transparent background is often solid black. That's what
                # was turning transparent backgrounds black the moment
                # this high-quality pass replaced the initial (correctly
                # transparent) Qt-scaled pixmap.
                src = Image.frombuffer('RGBA', (w, h), raw, 'raw', 'RGBA', 0, 1)
                resample = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
                src.thumbnail((target_w, target_h), resample)
                return src.tobytes('raw', 'RGBA'), src.width, src.height
            except Exception:
                return None
        future = ImageLoader._executor.submit(worker)
        def done(fut):
            try:
                result = fut.result()
            except Exception:
                result = None
            self.load_bridge.hq_resample.emit(generation, (result, target_w, target_h))
        future.add_done_callback(done)

    def _on_hq_resample_ready(self, generation, payload):
        self._hq_resample_inflight = False
        result, expected_w, expected_h = payload
        if generation != self.load_generation or not self.fit_to_window or self.current_movie:
            return
        # If the window was resized again while this was computing, a
        # newer pass is already scheduled (resizeEvent restarts the timer
        # on every resize event) -- skip this now-stale result rather than
        # briefly showing an image sized for the window's previous size.
        current_target = self.scroll_area.size() * self.devicePixelRatioF()
        if (abs(current_target.width() - expected_w) > 2
                or abs(current_target.height() - expected_h) > 2):
            return
        if not result:
            return
        raw, w, h = result
        qimg = QImage(raw, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pixmap = QPixmap.fromImage(qimg)
        if pixmap.isNull():
            return
        pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        self.image_label.setPixmap(pixmap)
        self.image_label.adjustSize()

    def toggle_actual_size(self):
        self.fit_to_window = not self.fit_to_window
        if self.fit_to_window:
            self.zoom_factor = 1.0
        # The fit-to-window cache may be a reduced decode; actual-size needs the full source.
        self.show_current_image()
        if self.fit_to_window:
            self._hq_resample_timer.start(250)
    
    def next_image(self):
        if self.image_list and self.current_index < len(self.image_list) - 1:
            self.current_index += 1
            self.show_current_image()
    
    def prev_image(self):
        if self.image_list and self.current_index > 0:
            self.current_index -= 1
            self.show_current_image()
    
    def _zoom_at(self, factor, global_pos=None):
        if not self.current_pixmap or self.current_pixmap.isNull():
            return

        if global_pos is None:
            from PyQt5.QtGui import QCursor
            global_pos = QCursor.pos()

        viewport = self.scroll_area.viewport()
        viewport_pos = viewport.mapFromGlobal(global_pos)

        # Capture the image-space point under the cursor before scaling.
        # For a large image this is simply viewport position + scroll offset.
        old_h = self.scroll_area.horizontalScrollBar().value()
        old_v = self.scroll_area.verticalScrollBar().value()
        label_pos = self.image_label.mapFrom(viewport, viewport_pos)
        anchor_x = label_pos.x()
        anchor_y = label_pos.y()

        self.fit_to_window = False
        old_zoom = self.zoom_factor
        new_zoom = max(0.05, min(16.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-6:
            return
        self.zoom_factor = new_zoom
        self.update_image_display()

        # Keep the same image pixel underneath the cursor.
        ratio = new_zoom / old_zoom
        new_anchor_x = anchor_x * ratio
        new_anchor_y = anchor_y * ratio
        target_h = int(new_anchor_x - viewport_pos.x())
        target_v = int(new_anchor_y - viewport_pos.y())
        self.scroll_area.horizontalScrollBar().setValue(target_h)
        self.scroll_area.verticalScrollBar().setValue(target_v)

    def zoom_in(self):
        self._zoom_at(1.20)
    
    def zoom_out(self):
        self._zoom_at(1.0 / 1.20)
    
    def toggle_fullscreen(self):
        self.show_cursor()
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self.reset_cursor_timer()
    
    def close_program(self):
        QTimer.singleShot(150, self.close)
    
    def show_image_list_dialog(self):
        if not self.image_list:
            return
        dialog = ImageListDialog(self.image_list, self.current_index, self, self.current_zip)
        if dialog.exec_() == QDialog.Accepted:
            selected = dialog.get_selected_index()
            if selected != self.current_index:
                self.current_index = selected
                self.show_current_image()
    
    def delete_image(self):
        if not self.image_list or self.current_zip:
            return
        current_file = self.image_list[self.current_index]
        try:
            os.remove(current_file)
            self.cache_manager.clear()
            self.image_list.pop(self.current_index)
            if self.current_index >= len(self.image_list):
                self.current_index = len(self.image_list) - 1
            if self.image_list:
                self.show_current_image()
            else:
                self.image_label.clear()
                self.filename_label.hide()
        except:
            pass
    
    def open_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, '이미지 열기', '',
            '이미지 파일 (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff *.ico);;ZIP 파일 (*.zip);;모든 파일 (*)'
        )
        if file_path:
            self.load_path(file_path)
    
    def toggle_slideshow(self):
        if self.slideshow_playing:
            self.stop_slideshow()
        else:
            self.start_slideshow()
    
    def start_slideshow(self):
        self.slideshow_playing = True
        self.slideshow_mode = self.settings.get('slideshow_mode', 'time')
        self.gif_max_loops = self.settings.get('slideshow_gif_loops', 2)
        self.gif_loop_count = 0
        self.gif_last_frame = -1
        if self.slideshow_mode == 'loop' and self.current_movie:
            self.connect_gif_loop()
        else:
            interval = self.settings.get('slideshow_interval', 3)
            self.slideshow.start(interval * 1000)
    
    def stop_slideshow(self):
        self.slideshow_playing = False
        self.slideshow.stop()
        if self.gif_frame_connected and self.current_movie:
            try:
                self.current_movie.frameChanged.disconnect(self.on_gif_frame_changed)
            except:
                pass
            self.gif_frame_connected = False
    
    def show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #2b2b2b; color: white; border: 1px solid #555; }
            QMenu::item:selected { background-color: #3c3c3c; }
        """)
        slideshow_action = QAction('슬라이드쇼', self)
        slideshow_action.triggered.connect(self.toggle_slideshow)
        menu.addAction(slideshow_action)
        menu.addSeparator()
        settings_action = QAction('설정', self)
        settings_action.triggered.connect(self.show_settings)
        menu.addAction(settings_action)
        shortcuts_action = QAction('단축키 설정', self)
        shortcuts_action.triggered.connect(self.show_shortcut_settings)
        menu.addAction(shortcuts_action)
        menu.addSeparator()
        close_action = QAction('프로그램 종료', self)
        close_action.triggered.connect(self.close_program)
        menu.addAction(close_action)
        menu.exec_(self.mapToGlobal(pos))
    
    def show_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec_():
            self.preload_enabled = self.settings.get('preload_next', True)
            self.preload_count = max(0, min(10, int(self.settings.get('preload_count', 3))))
            self.apply_background_color()
            # See apply_image_adjustments: cache keys already scope by
            # saturation/brightness/contrast/size, so no explicit clear is
            # needed here either -- it would only discard reusable entries.
            if self.image_list:
                self.show_current_image()
    
    def show_shortcut_settings(self):
        dialog = ShortcutSettingsDialog(self.settings, self)
        if dialog.exec_():
            pass
    
    def _can_pan_image(self):
        if not self.current_pixmap or self.fit_to_window:
            return False
        viewport = self.scroll_area.viewport().size()
        label_size = self.image_label.size()
        return (label_size.width() > viewport.width() + 1 or
                label_size.height() > viewport.height() + 1)

    def _start_image_pan(self, global_pos):
        if not self._can_pan_image():
            return False
        self.panning = True
        self.pan_start_pos = QPoint(global_pos)
        self.pan_start_h = self.scroll_area.horizontalScrollBar().value()
        self.pan_start_v = self.scroll_area.verticalScrollBar().value()
        self.show_cursor()
        self.setCursor(Qt.ClosedHandCursor)
        return True

    def _move_image_pan(self, global_pos):
        if not self.panning or self.pan_start_pos is None:
            return False
        delta = QPoint(global_pos) - self.pan_start_pos
        # Move in the same direction as the hand drag. Scrollbar values are
        # therefore decreased by the mouse delta. Qt clamps them to valid bounds.
        self.scroll_area.horizontalScrollBar().setValue(self.pan_start_h - delta.x())
        self.scroll_area.verticalScrollBar().setValue(self.pan_start_v - delta.y())
        return True

    def _end_image_pan(self):
        if not self.panning:
            return False
        self.panning = False
        self.pan_start_pos = None
        self.show_cursor()
        self.setCursor(Qt.ArrowCursor)
        self.reset_cursor_timer()
        return True

    def _is_pan_target(self, widget):
        if widget is None:
            return False
        viewport = self.scroll_area.viewport()
        if widget is self.image_label or widget is viewport:
            return True
        try:
            # widgetAt() can return a child widget inside the viewport.
            # What matters is whether the pointer is inside our image area.
            return viewport.isAncestorOf(widget) or self.image_label.isAncestorOf(widget)
        except Exception:
            return False

    def _handle_tilt_wheel(self, event):
        dx = event.angleDelta().x()
        if dx == 0:
            return False
        # The mouse reports horizontal tilt with the opposite sign on this
        # device/event path. Map the physical direction to the UI name.
        button_text = 'Tilt Left' if dx > 0 else 'Tilt Right'
        self.check_mouse_shortcut(button_text)
        event.accept()
        return True

    def eventFilter(self, obj, event):
        # The image label / scroll-area viewport sit directly under the
        # cursor and cover the whole window, so they receive wheel and
        # mouse-move events before QMainWindow ever would. Handle wheel
        # tilt, the resize cursor, and the auto-hide timer here directly
        # instead of relying on those reaching wheelEvent/mouseMoveEvent.
        if event.type() == QEvent.Wheel:
            if self._handle_tilt_wheel(event):
                return True
        elif event.type() == QEvent.MouseMove:
            if not (self.dragging or self.resizing or self.panning):
                self.update_cursor(obj.mapTo(self, event.pos()))
            self.show_cursor()
            self.reset_cursor_timer()
        return super().eventFilter(obj, event)

    def wheelEvent(self, event: QWheelEvent):
        if self._handle_tilt_wheel(event):
            # Tilt wheel acts as a button shortcut, so it still wakes the cursor.
            self.show_cursor()
            self.reset_cursor_timer()
            return
        # Plain up/down wheel scroll (prev/next image) must NOT un-hide the
        # cursor while it's hidden — applies in both windowed and fullscreen
        # mode since this handler is shared by both.
        if event.angleDelta().y() > 0:
            self.prev_image()
        else:
            self.next_image()
        event.accept()
    
    def mousePressEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        region = self.get_resize_region(event.pos())
        if event.button() == Qt.LeftButton and region and not self.isFullScreen():
            self.resizing = True
            self.resize_start_pos = event.globalPos()
            self.resize_start_size = self.size()
            self.resize_region = region
            event.accept()
            return
        if event.button() == Qt.LeftButton and not region:
            # Fullscreen has no window-position dragging.
            if self.isFullScreen():
                event.accept()
                return
            self.dragging = True
            self.drag_start_pos = event.globalPos()
            self.window_start_pos = self.pos()
            event.accept()
            return
        button_text = ''
        if event.button() == Qt.MiddleButton:
            button_text = 'Middle Click'
        elif event.button() == Qt.XButton1:
            button_text = 'XButton1'
        elif event.button() == Qt.XButton2:
            button_text = 'XButton2'
        if button_text:
            self.check_mouse_shortcut(button_text)
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        
        if self.resizing and self.resize_start_pos:
            delta = event.globalPos() - self.resize_start_pos
            new_w = self.resize_start_size.width()
            new_h = self.resize_start_size.height()
            if self.resize_region in ['left', 'topleft', 'bottomleft']:
                new_w = max(200, self.resize_start_size.width() - delta.x())
            elif self.resize_region in ['right', 'topright', 'bottomright']:
                new_w = max(200, self.resize_start_size.width() + delta.x())
            if self.resize_region in ['top', 'topleft', 'topright']:
                new_h = max(150, self.resize_start_size.height() - delta.y())
            elif self.resize_region in ['bottom', 'bottomleft', 'bottomright']:
                new_h = max(150, self.resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)
            event.accept()
            return
        if self.dragging and self.drag_start_pos and not self.isFullScreen():
            delta = event.globalPos() - self.drag_start_pos
            new_pos = self.window_start_pos + delta
            new_pos = self.snap_to_edge(new_pos)
            self.move(new_pos)
            event.accept()
            return
        self.update_cursor(event.pos())
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        if event.button() == Qt.LeftButton and self.resizing:
            self.resizing = False
            self.resize_start_pos = None
            self.resize_start_size = None
            self.resize_region = None
            self.unsetCursor()
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            self.drag_start_pos = None
            self.window_start_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)
    
    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self.show_cursor()
        self.reset_cursor_timer()
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.check_mouse_shortcut('Left Double Click')
        elif event.button() == Qt.RightButton:
            self.dragging = False
            self.check_mouse_shortcut('Right Double Click')
        super().mouseDoubleClickEvent(event)
    
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_to_window and not self._display_update_timer.isActive():
            self._display_update_timer.start(8)
        if self.fit_to_window:
            # Restarted (not just started-if-idle like the timer above) on
            # every resize event, so it only actually fires once resizing
            # has been quiet for the delay -- redoing a Lanczos resample on
            # every single resize event during an active drag would make
            # the drag itself feel laggy, which is a worse trade than a
            # brief moment of slightly-softer image right after a resize
            # or fit-to-window toggle.
            self._hq_resample_timer.start(250)
    
    def closeEvent(self, event: QCloseEvent):
        self.show_cursor()
        if self.isFullScreen():
            self.showNormal()
        if not self.isFullScreen():
            self.save_settings()
        self.stop_current_movie()
        self.slideshow.stop()
        self.cursor_hide_timer.stop()
        # Stop creating new background work and release all worker threads.
        # This is important on Windows: ThreadPoolExecutor worker threads can
        # keep the process alive and retain large decoded images/ZIP handles.
        try:
            self.load_generation += 1
            self.loading_keys.clear()
            self.cache_manager.clear()
            self.current_pixmap = None
            self.original_pixmap = None
        except Exception:
            pass
        try:
            ImageLoader.shutdown_executor()
        except Exception:
            pass
        try:
            ZipHandler._thread_local.__dict__.clear()
        except Exception:
            pass
        try:
            self.gl_color_corrector.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    icon_path = get_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    
    single_app = SingleApplication()
    if single_app.is_running():
        if len(sys.argv) > 1:
            single_app.send_message(sys.argv[1])
        sys.exit(0)
    single_app.start_server()
    viewer = ImageViewer()
    single_app.set_file_received_callback(viewer.load_path)
    if len(sys.argv) > 1:
        viewer.load_path(sys.argv[1])
    viewer.show()
    QTimer.singleShot(100, viewer.force_foreground)
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
