import sys
import os
import json
import threading
import re
import time
import struct
import queue
import ctypes
import concurrent.futures
from functools import lru_cache
from io import BytesIO
from collections import OrderedDict

from PyQt5.QtWidgets import (QApplication, QMainWindow, QLabel, QScrollArea,
                            QMenu, QAction, QFileDialog, QVBoxLayout, QWidget,
                            QDialog, QHBoxLayout, QComboBox, QCheckBox, QPushButton,
                            QColorDialog, QGroupBox, QFormLayout, QSpinBox,
                            QGridLayout,
                            QListWidget, QListWidgetItem, QMessageBox,
                            QListView, QSlider, QSplitter)
from PyQt5.QtCore import Qt, QTimer, QObject, QByteArray, QSize, pyqtSignal, QPoint, QEvent, QBuffer, QIODevice
from PyQt5.QtGui import (QImage, QPixmap, QKeySequence, QWheelEvent, QImageReader,
                        QMovie, QKeyEvent, QCloseEvent, QMouseEvent, QIcon, QColor,
                        QOpenGLContext, QOffscreenSurface, QOpenGLFramebufferObject,
                        QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QVector2D)
from PyQt5.QtNetwork import QLocalSocket, QLocalServer

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PIL_Image = None

def get_pil_image():
    global PIL_Image
    if PIL_Image is None:
        from PIL import Image
        PIL_Image = Image
    return PIL_Image

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

_warmup_done = set()
_warmup_lock = threading.Lock()

def _warm_up(kind, loaders):
    with _warmup_lock:
        if kind in _warmup_done:
            return
        _warmup_done.add(kind)

    def run():
        for loader in loaders:
            try:
                loader()
            except Exception:
                pass

    threading.Thread(target=run, name=f'import-warmup-{kind}', daemon=True).start()

def request_cv2_warmup():
    """Import numpy + cv2 in the background, once. The webp fast path only
    switches on after this has finished; until then images simply go through
    Qt's own reader, instead of a decode worker stalling on a cold import."""
    _warm_up('cv2', (get_pil_image, get_numpy, get_cv2))

def warm_up_for_first_file(ext):
    """Pillow (and, for webp, numpy + cv2) are imported lazily on first use and
    a cold import costs tens to hundreds of ms. For a gif/webp that first use
    lands on the GUI thread (get_frame_count) and would hold up the very first
    image, so those imports are started on a background thread as soon as the
    window is up, overlapping with Qt's first paint and the first decode.
    Nothing is imported for a plain jpg/png, which never needs them."""
    if ext in ('.gif', '.webp'):
        _warm_up('pil', (get_pil_image,))
    if ext == '.webp':
        request_cv2_warmup()

# Windows file-association support (see FileAssociationDialog). All of
# this only ever touches HKEY_CURRENT_USER, never HKEY_LOCAL_MACHINE --
# per-user file associations don't need administrator rights, and this
# way a failure here can never require elevation to recover from.
_FILE_ASSOC_PROG_ID = 'PekoviewerApp.Image'

def _get_app_launch_command():
    """Command line to register for a file association's "open" action.
    sys.frozen is the standard way a PyInstaller-built .exe marks itself
    at runtime; running from source instead needs the interpreter *and*
    this script's own path, not just the interpreter."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" "%1"'
    script_path = os.path.abspath(sys.argv[0])
    return f'"{sys.executable}" "{script_path}" "%1"'

def is_extension_associated(ext):
    """True only if this app itself currently owns ext's association --
    never true for an association some other app or the system owns, so
    checkbox state in FileAssociationDialog always reflects reality
    rather than assuming."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f'Software\\Classes\\{ext}') as key:
            value, _ = winreg.QueryValueEx(key, '')
            return value == _FILE_ASSOC_PROG_ID
    except OSError:
        return False

def set_extension_association(ext, associate):
    """Register (associate=True) or unregister (False) ext to open with
    this app. Unregistering only ever deletes the .ext key when it's
    currently pointing at this app's own prog ID -- so toggling a
    checkbox off can never disturb an association that belongs to a
    different application, only ever undo what this dialog itself set.
    Returns True on success; on any registry error, prints and returns
    False so the caller can revert the checkbox instead of leaving it
    showing a state that was never actually applied."""
    import winreg
    try:
        if associate:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f'Software\\Classes\\{ext}') as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, _FILE_ASSOC_PROG_ID)
            prog_key_path = f'Software\\Classes\\{_FILE_ASSOC_PROG_ID}\\shell\\open\\command'
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, prog_key_path) as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, _get_app_launch_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f'Software\\Classes\\{ext}', 0, winreg.KEY_READ) as key:
                    current, _ = winreg.QueryValueEx(key, '')
                if current == _FILE_ASSOC_PROG_ID:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f'Software\\Classes\\{ext}')
            except FileNotFoundError:
                pass
        try:
            # Tells Explorer the association table changed, so it picks
            # this up immediately instead of needing a sign-out/in.
            # SHCNE_ASSOCCHANGED, SHCNF_IDLIST -- failure here doesn't
            # mean the registry change itself failed, so it's not
            # allowed to turn this whole call into a reported failure.
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"파일 연결 설정 오류 ({ext}): {e}")
        return False

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
    is a single 3x4 color-matrix convert() (_saturate_matrix): same blend
    math as ImageEnhance.Color, ~20-35% faster, output within +/-1.
    The contrast LUT's pivot is computed from the *current* image (after
    saturation/brightness were already applied, same as ImageEnhance does
    internally) via PIL's own fast ImageStat, so the sequential-clipping
    behavior matches too.
    """
    if saturation != 100:
        img = _saturate_matrix(img, saturation)
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
        # Preserve the source alpha instead of forcing full opacity --
        # this used to hardcode 255 here (matching the PIL/GPU paths,
        # which also used to discard alpha), which turned any
        # transparent area solid-colored once the underlying RGB value
        # a transparent pixel happens to store (often black) was no
        # longer masked by transparency.
        out[:, :, 3] = arr[:, :, 3]
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
            vec4 texColor = texture2D(u_texture, v_texcoord);
            vec3 color = texColor.rgb;
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
            // Pass the source alpha through unchanged -- this used to be
            // hardcoded to 1.0 (fully opaque), which silently destroyed
            // any transparency in the source frame: a transparent pixel's
            // RGB is often undefined/black once alpha is gone, so forcing
            // opacity here baked that black in permanently instead of
            // keeping the pixel see-through.
            gl_FragColor = vec4(color, texColor.a);
        }
    """

    _GL_TRIANGLE_STRIP = 0x0005

    def __init__(self):
        self._context = None
        self._surface = None
        self._program = None
        # Bound via ctypes directly against opengl32.dll instead of PyQt5's
        # own OpenGL-functions wrapper -- see _ensure_ready for why.
        self._gl_viewport = None
        self._gl_draw_arrays = None
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
                # Neither context.functions() nor PyQt5.QtGui.QOpenGLFunctions
                # can be relied on -- confirmed from real logs that
                # context.functions() raises "'QOpenGLContext' object has no
                # attribute 'functions'" on one PyQt5 build, and importing
                # QOpenGLFunctions itself raises ImportError on another (it
                # simply isn't exposed there). Binding the two GL calls this
                # class actually needs straight from opengl32.dll via ctypes
                # sidesteps PyQt5's OpenGL-functions wrapper entirely --
                # glViewport/glDrawArrays are core GL 1.1 entry points
                # exported directly by every Windows opengl32.dll, and they
                # operate on whatever context is current on this thread
                # (set via context.makeCurrent above), regardless of which
                # library issued the call. ctypes is already a hard
                # dependency of this app (see the top-level ctypes.windll
                # usage), so this adds nothing new.
                gl32 = ctypes.windll.opengl32
                gl_viewport = gl32.glViewport
                gl_viewport.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
                gl_viewport.restype = None
                gl_draw_arrays = gl32.glDrawArrays
                gl_draw_arrays.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
                gl_draw_arrays.restype = None
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
            self._gl_viewport = gl_viewport
            self._gl_draw_arrays = gl_draw_arrays
            return True
        except Exception as e:
            print(f"GPU 색보정 초기화 실패, 이후 프레임은 cv2/PIL 경로를 사용합니다: {e}")
            self._broken = True
            self._context = None
            self._surface = None
            self._program = None
            self._gl_viewport = None
            self._gl_draw_arrays = None
            return False

    def adjust(self, qimage, saturation, brightness, contrast, target_w, target_h):
        """qimage: source frame, any QImage format. saturation/brightness/
        contrast: 1.0 = no change. Returns a QImage sized target_w x
        target_h with the source alpha preserved per-pixel (matching
        apply_color_adjustments/apply_color_adjustments_cv2, which do the
        same), or None if the GPU path isn't available, in which case the
        caller should fall back to the cv2/Pillow tiers. Callers pass the
        frame's own native size as target_w/target_h now, not the
        on-screen display size -- see _render_animated_frame_gpu."""
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
            self._gl_viewport(0, 0, target_w, target_h)

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

            self._gl_draw_arrays(self._GL_TRIANGLE_STRIP, 0, 4)

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

@lru_cache(maxsize=None)
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

_app_icon = None

def get_app_icon():
    """One shared QIcon, loaded once. The window, the application and every
    show event each used to re-run the path search and re-read icon.ico."""
    global _app_icon
    if _app_icon is None:
        path = get_icon_path()
        _app_icon = QIcon(path) if path else QIcon()
    return _app_icon

_NATURAL_SPLIT = re.compile(r'(\d+)').split

def natural_sort_key(s):
    # Splitting on a digit run puts the numbers at the odd positions. (Telling
    # numbers apart with str.isdigit() instead also accepts characters like
    # '²' that int() rejects, which made the sort -- and with it the whole
    # folder listing -- fail for a file name containing one.)
    return [int(p) if i & 1 else p.lower() for i, p in enumerate(_NATURAL_SPLIT(s))]

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

# Once an animated webp's first loop has put every frame into
# animated_frame_cache, later loops are shown straight from that cache on
# our own timer, with QMovie paused (see
# ImageViewer._try_start_anim_cache_playback). QMovie decodes (and scales)
# every frame again on every loop even when we then ignore the result in
# favor of the cached one, which is why the 2nd, 3rd, ... loop of a
# high-res webp was no faster than the first.

# Keep the finished frame cache of an animation you navigate away from (up to
# ANIMATED_CACHE_RETAIN_MAX of them, only while there's RAM to spare and
# "주변 이미지 미리 로딩" is on), so coming back to it replays from the cache at
# once instead of decoding the whole first loop again -- the same way an
# image you leave stays cached and shows instantly when you return.
ANIMATED_CACHE_RETAIN_MAX = 2

def read_webp_animation_info(source):
    """Per-frame display durations (ms) and loop count of an animated WebP,
    read straight from its RIFF container -- no pixel decoding, so it costs a
    handful of chunk-header reads even for a huge file.

    source is the raw bytes (a zip entry) or a path on disk. Returns
    (durations, loop_count) -- loop_count 0 means "loop forever", as in the
    WebP container spec -- or None if this isn't a well-formed animated WebP.
    Read from the file itself, rather than sampled off QMovie while the first
    loop plays, so cached replay timing doesn't depend on how long each frame
    happened to take to decode."""
    try:
        f = BytesIO(source) if isinstance(source, (bytes, bytearray)) else open(source, 'rb')
        with f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b'RIFF' or head[8:12] != b'WEBP':
                return None
            end = 8 + int.from_bytes(head[4:8], 'little')
            durations = []
            loop_count = 0
            pos = 12
            while pos + 8 <= end:
                f.seek(pos)
                chunk = f.read(8)
                if len(chunk) < 8:
                    break
                tag = chunk[:4]
                size = int.from_bytes(chunk[4:8], 'little')
                if tag == b'ANIM':
                    # background color (4 bytes), then loop count (2 bytes)
                    body = f.read(6)
                    if len(body) == 6:
                        loop_count = int.from_bytes(body[4:6], 'little')
                elif tag == b'ANMF':
                    # x, y, width-1, height-1 (3 bytes each), then the
                    # frame duration (3 bytes), then a flags byte
                    body = f.read(16)
                    if len(body) < 16:
                        return None
                    durations.append(int.from_bytes(body[12:15], 'little'))
                pos += 8 + size + (size & 1)
            return (durations, loop_count) if durations else None
    except Exception:
        return None

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', ctypes.c_uint32),      # DWORD (fixed 4 bytes, unlike c_ulong off Windows)
        ('dwMemoryLoad', ctypes.c_uint32),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]

def get_available_physical_memory():
    """Bytes of physical RAM that can be handed out right now without
    paging anything to disk (free + standby lists), or None if Windows
    won't say."""
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    except Exception:
        pass
    return None

# Never smaller than the fixed 4GB this used to be, never larger than 24GB.
_ANIM_CACHE_BUDGET_FLOOR = 4096 * 1024 * 1024
_ANIM_CACHE_BUDGET_CAP = 24 * 1024 * 1024 * 1024

def animated_cache_budget_bytes(avail=None, extra_bytes=0):
    """How much memory one animation's frame cache may use: 80% of the RAM
    that's actually free right now, clamped to [4GB, 24GB]. Cached replay
    (ImageViewer._try_start_anim_cache_playback) only works if the *whole*
    loop fits, so a fixed ceiling that a long high-res animation happens
    to exceed silently turns it off for that animation; following the free
    RAM instead lets it engage whenever the machine can really hold the
    loop.

    avail is the free-RAM reading to use (a fresh one when omitted) and
    extra_bytes memory that is about to be handed back on top of it: a
    caller that drops retained caches to make room passes the one reading it
    took before dropping anything plus what it dropped, rather than reading
    again afterwards and counting the same memory twice."""
    if avail is None:
        avail = get_available_physical_memory()
    if not avail:
        return _ANIM_CACHE_BUDGET_FLOOR
    return int(min(max((avail + extra_bytes) * 0.8, _ANIM_CACHE_BUDGET_FLOOR), _ANIM_CACHE_BUDGET_CAP))

def animated_preload_budget_bytes(avail=None):
    """How much memory a *speculative* neighbor preload may claim: 60% of
    the RAM that is free right now, capped, and -- unlike
    animated_cache_budget_bytes -- with no floor. That function's 4GB floor
    is right for the animation actually on screen (it has to be cached to
    play smoothly), but pushing a machine that has less than that free into
    paging for something nobody asked to see yet just makes the animation
    that IS playing hitch. Memory already held by retained caches is not
    counted as available (it is already resident, so it is already
    reflected in what's free); the 40% left over is headroom for the rest
    of the system. Returns 0 (don't preload) if free RAM can't be read."""
    if avail is None:
        avail = get_available_physical_memory()
    if avail is None:
        return 0
    return int(min(0.6 * max(0, avail), _ANIM_CACHE_BUDGET_CAP))

def animated_cache_frame_limit(frame_count, frame_w, frame_h, budget_bytes):
    """How many frames of an animation the frame cache may hold: the whole
    loop if it fits in budget_bytes at frame_w x frame_h (4 bytes/pixel),
    otherwise as many as do (at least 24), and never more than 3000."""
    bytes_per_frame = max(1, frame_w * frame_h * 4)
    by_memory = max(1, budget_bytes // bytes_per_frame)
    return max(24, min(frame_count, by_memory, 3000))

def compute_anim_decode_size(orig_w, orig_h, fit_to_window, box_w, box_h, zoom_factor):
    """Pure-arithmetic twin of the size the live path decodes an animation
    at (show_current_image's scaled_size, then ImageViewer._anim_decode_
    size), parameterized by an arbitrary native size instead of always
    reading self.current_movie_original_size -- so ImageViewer.
    _preload_neighbor_animations can work out the size a *neighbor* file
    would use (its native size can differ from the file on screen) without
    constructing a QMovie for it, and without touching any Qt object from
    a worker thread: box_w/box_h (already dpr-scaled, taken from the same
    QSize * dpr the live path uses) and zoom_factor are plain numbers read
    on the GUI thread and handed in, and this function does no Qt calls.

    The result has to match the live path to the pixel, not just roughly:
    a preloaded frame that is even one pixel off the on-screen target is
    silently re-scaled (smooth, per frame) every time it is shown by
    _show_animated_pixmap. So fit_to_window uses QSize.scaled(box,
    Qt.KeepAspectRatio)'s own integer arithmetic (not a float scale that
    can land a pixel away), and not fit_to_window scales by zoom_factor
    exactly as the live path does. Either way the result is capped at the
    native size when it would exceed it, like _anim_decode_size.
    Returns (w, h)."""
    if orig_w <= 0 or orig_h <= 0:
        return orig_w, orig_h
    if fit_to_window and box_w and box_h:
        rw = box_h * orig_w // orig_h
        if rw <= box_w:
            tw, th = rw, box_h
        else:
            tw, th = box_w, box_w * orig_h // orig_w
    else:
        tw, th = int(orig_w * zoom_factor), int(orig_h * zoom_factor)
    if tw <= 0 or th <= 0:
        return orig_w, orig_h
    if tw <= orig_w and th <= orig_h:
        return tw, th
    return orig_w, orig_h

# SetThreadPriority's dedicated "background processing mode" (Vista+):
# lowers the calling thread's CPU scheduling priority *and* its I/O and
# memory priority together, and can only target the calling thread itself.
_THREAD_MODE_BACKGROUND_BEGIN = 0x00010000

def _lower_current_thread_to_background_priority():
    """Drop the CALLING thread's OS-level priority for the rest of its
    life. Used by ImageLoader._anim_preload_executor's worker (see
    _submit_anim_preload): that pool has exactly one persistent thread
    (_ANIM_PRELOAD_WORKER_COUNT=1), doing sustained, back-to-back decode +
    resize + color-adjust work across hundreds of frames per neighbor --
    heavy enough, even confined to its own thread pool, that plain OS
    scheduling could still let it compete with the GUI thread for CPU
    right when the GUI thread's own frame timer needs it, which is what
    caused the playback stutter this preload feature introduced. Lowering
    this thread's priority (once, permanently -- there's no foreground
    work this specific thread ever needs to do) asks Windows to prefer the
    GUI thread whenever both want the CPU at once.

    Safe to call on every task this thread ever runs: once it has entered
    background mode, the call just fails harmlessly on every later call
    (nothing to undo it, since this thread never does anything else)."""
    try:
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _THREAD_MODE_BACKGROUND_BEGIN)
    except Exception:
        pass

def decode_webp_animation_frames(source, frame_count, target_w, target_h,
                                  saturation, brightness, contrast, is_cancelled):
    """_decode_webp_animation_frames_pillow 와 같은 결과를 내는데, 먼저 더 빠른 경로를 시도한다:
    webp 를 프레임 단위로 잘라서 (전체 캔버스를 덮는 알파 없는 프레임은 cv2 로 한 번에,
    나머지는 키프레임 구간 단위로 Pillow 로) 푼다 -- 첫 루프 병렬 디코딩 (FirstLoopJob) 이 쓰는
    것과 같은 작업 계획이고, 여기서는 이 스레드 하나에서 순서대로 푼다. 구조를 못 읽거나 도중에
    실패하면 예전 Pillow 순차 디코딩으로 처음부터 다시 한다. 취소되면 None."""
    try:
        plan = _WebpDecodePlan.build(source, frame_count, target_w, target_h,
                                     saturation, brightness, contrast)
        if plan is not None:
            frames = [None] * frame_count

            def emit(idx, raw, w, h, opaque):
                frames[idx] = (raw, w, h)

            for item in plan.items:
                if is_cancelled():
                    return None
                plan.decode_item(item, emit, is_cancelled)
            if all(f is not None for f in frames):
                return frames
    except _DecodeCancelled:
        return None
    except Exception:
        pass
    if is_cancelled():
        return None
    return _decode_webp_animation_frames_pillow(source, frame_count, target_w, target_h,
                                                saturation, brightness, contrast, is_cancelled)


def _decode_webp_animation_frames_pillow(source, frame_count, target_w, target_h,
                                         saturation, brightness, contrast, is_cancelled):
    """Fully decode+scale+color-adjust every frame of an animated image via
    Pillow, entirely off the GUI thread -- no QMovie/QImage/QPixmap here
    (Qt requires pixmap creation on the GUI thread; see
    ImageViewer._anim_admit_step_impl for where this raw result
    becomes QPixmaps). Used to pre-decode an eligible neighbor's whole
    loop in the background (ImageViewer._preload_neighbor_animations)
    using the same resize-then-color-adjust order and math as the live
    per-frame path (_process_animated_frame_fast / apply_color_
    adjustments: native decode, resize to (target_w, target_h) -- mirrors
    QMovie's own setScaledSize() decode -- then color-adjust at that
    size), so a preloaded frame matches one decoded live.

    is_cancelled is polled between frames; once it returns True this stops
    and returns None -- used when a neighbor falls out of the preload
    range before its decode finishes (see _preload_neighbor_animations).

    Returns a list of (raw_rgba_bytes, w, h) tuples, one per frame in
    order, or None on any failure or cancellation. All-or-nothing: a
    partial loop is no use to ImageViewer._take_retained_anim_cache, which
    requires every frame present."""
    try:
        Image = get_pil_image()
        frames = []
        with Image.open(source) as im:
            for i in range(frame_count):
                if is_cancelled():
                    return None
                im.seek(i)
                frame = im.convert('RGBA')
                if frame.size != (target_w, target_h):
                    frame = frame.resize((target_w, target_h), Image.BILINEAR)
                w, h = frame.size
                raw = frame.tobytes('raw', 'RGBA')
                if saturation == 100 and brightness == 100 and contrast == 100:
                    frames.append((raw, w, h))
                    continue
                result = _process_animated_frame_fast(raw, w, h, saturation, brightness, contrast, None, None)
                if result is not None:
                    frames.append(result)
                    continue
                # Pillow fallback -- mirrors _submit_animated_frame_processing's
                # own fallback for when cv2 isn't installed.
                alpha = frame.getchannel('A')
                rgb = frame.convert('RGB')
                rgb = apply_color_adjustments(rgb, saturation, brightness, contrast)
                out = rgb.convert('RGBA')
                out.putalpha(alpha)
                frames.append((out.tobytes('raw', 'RGBA'), rgb.width, rgb.height))
        if len(frames) != frame_count:
            return None
        return frames
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 고해상도 애니메이션 webp: 첫 루프를 작업 스레드 여러 개로 미리 디코딩
# ---------------------------------------------------------------------------
# 예전에는 첫 루프를 QMovie 가 GUI 스레드에서 프레임 하나씩 디코딩했다. 프레임
# 하나를 푸는 시간이 프레임 간격보다 길면 그만큼 느려지고 (3600x2688 / 186프레임
# 파일: 프레임당 약 124ms 대 간격 33ms) UI 도 같이 멈칫거린다. 캐시 재생 덕분에
# 2번째 루프부터는 빠르지만, 첫 루프는 어떤 식으로든 모든 프레임을 한 번은 풀어야
# 한다.
#
# 그래서 이런 파일은 QMovie 를 시작하지 않고, 작업 스레드 여러 개가 루프 전체를
# 풀어서 animated_frame_cache 를 채운다 (ImageViewer._start_first_loop_decode).
# 재생은 기존 캐시 재생 (_anim_cache_tick) 이 맡고, 디코딩이 재생을 따라잡을 수
# 있다고 판단되는 시점 (first_loop_can_start) 부터 시작한다.
#
# 속도를 내는 방법은 두 가지다.
#  1) 병렬: 앞 프레임 없이 혼자 디코딩되는 프레임 (libwebp 의 "키프레임") 은
#     서로 독립이라 여러 스레드가 나눠 풀 수 있다. 덜 독립적인 프레임은 키프레임
#     에서 시작하는 구간 단위로 순서대로 푼다.
#  2) 프레임 하나의 비용 줄이기: 전체 캔버스를 덮는 알파 없는 프레임은 단독 webp
#     파일로 잘라서 cv2.imdecode 로 한 번에 풀고 cv2.resize (INTER_AREA) 로 줄인다.
#     Pillow 의 애니메이션 디코더는 프레임마다 캔버스 전체 복사를 여러 번 하고 (3600x2688
#     에서 프레임당 약 120ms) 리사이즈도 훨씬 느리다. 네이티브 크기 결과는 Pillow 와
#     픽셀 단위로 같다.
#
# 구조가 맞지 않거나 (webp 가 아님, 프레임 수 불일치 ...) 어떤 단계든 실패하면
# 기존 QMovie 재생으로 그대로 되돌아간다.

# 원본 프레임의 픽셀 수가 이보다 작으면 (QMovie 로도 충분히 빠르므로) 예전 방식 그대로.
ANIMATED_FIRST_LOOP_MIN_PIXELS = 1500000
ANIMATED_FIRST_LOOP_MIN_FRAMES = 8
# 재생이 따라잡을 수 있을 때까지 기다리는 최대 시간(초). 넘으면 덜 채워졌어도 재생을
# 시작하고, 그때부터는 아직 안 풀린 프레임을 만날 때마다 그 프레임이 나올 때까지 기다린다.
ANIMATED_FIRST_LOOP_MAX_PREBUFFER = 3.0

# GUI 스레드와 OS 용으로 코어 2개는 남겨 둔다.
_FIRST_LOOP_WORKER_COUNT = max(2, min(8, (os.cpu_count() or 4) - 2))


class _DecodeCancelled(Exception):
    pass


def _set_current_thread_below_normal_priority():
    """작업 스레드가 GUI 스레드보다 먼저 CPU 를 가져가지 않도록
    THREAD_PRIORITY_BELOW_NORMAL 로 낮춘다 (호출한 스레드에만 적용)."""
    try:
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)
    except Exception:
        pass


def _webp_frame_has_alpha(data, pos, end):
    """ANMF 안쪽 이미지 청크에 알파가 있는지 (libwebp 의 has_alpha 와 같은 기준)."""
    alpha = False
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], 'little')
        if tag == b'ALPH':
            alpha = True
        elif tag == b'VP8L' and pos + 13 <= len(data) and data[pos + 8] == 0x2F:
            if (int.from_bytes(data[pos + 9:pos + 13], 'little') >> 28) & 1:
                alpha = True
        pos += 8 + size + (size & 1)
    return alpha


def parse_animated_webp(data):
    """애니메이션 webp 의 RIFF 컨테이너에서 프레임 표를 만든다 (픽셀 디코딩 없음).

    반환: {'vp8x': 바이트, 'anim': 바이트, 'canvas': (w, h), 'frames': [...]}
    프레임 항목은 start/length (ANMF 청크 전체), inner (안쪽 이미지 청크 범위), w/h/x/y,
    dur, no_blend, dispose_bg, alpha. 구조가 이상하면 None."""
    try:
        if len(data) < 30 or data[:4] != b'RIFF' or data[8:12] != b'WEBP':
            return None
        end = min(len(data), 8 + int.from_bytes(data[4:8], 'little'))
        pos = 12
        vp8x = anim = canvas = None
        frames = []
        while pos + 8 <= end:
            tag = data[pos:pos + 4]
            size = int.from_bytes(data[pos + 4:pos + 8], 'little')
            total = 8 + size + (size & 1)
            if pos + 8 + size > end:
                return None
            if tag == b'VP8X':
                if size < 10:
                    return None
                vp8x = data[pos:pos + total]
                canvas = (int.from_bytes(data[pos + 12:pos + 15], 'little') + 1,
                          int.from_bytes(data[pos + 15:pos + 18], 'little') + 1)
            elif tag == b'ANIM':
                anim = data[pos:pos + total]
            elif tag == b'ANMF':
                if size < 16 + 8:
                    return None
                b = pos + 8
                flags = data[b + 15]
                frames.append({
                    'start': pos, 'length': total, 'inner': (b + 16, pos + 8 + size),
                    'x': 2 * int.from_bytes(data[b:b + 3], 'little'),
                    'y': 2 * int.from_bytes(data[b + 3:b + 6], 'little'),
                    'w': int.from_bytes(data[b + 6:b + 9], 'little') + 1,
                    'h': int.from_bytes(data[b + 9:b + 12], 'little') + 1,
                    'dur': int.from_bytes(data[b + 12:b + 15], 'little'),
                    'dispose_bg': bool(flags & 1),
                    'no_blend': bool(flags & 2),
                    'alpha': _webp_frame_has_alpha(data, b + 16, pos + 8 + size),
                })
            pos += total
        if not (vp8x and anim and canvas and frames):
            return None
        return {'vp8x': vp8x, 'anim': anim, 'canvas': canvas, 'frames': frames}
    except Exception:
        return None


def _webp_keyframe_flags(frames, cw, ch):
    """libwebp anim_decode.c 의 IsKeyFrame 과 같은 판정. 키프레임은 앞 프레임의
    내용과 상관없이 (캔버스를 투명하게 비운 채로) 혼자 디코딩되는 프레임이다."""
    keys = []
    prev_key = False
    for i, f in enumerate(frames):
        full = f['w'] == cw and f['h'] == ch
        if i == 0:
            k = True
        elif (not f['alpha'] or f['no_blend']) and full:
            k = True
        else:
            p = frames[i - 1]
            p_full = p['w'] == cw and p['h'] == ch
            k = p['dispose_bg'] and (p_full or prev_key)
        keys.append(bool(k))
        prev_key = k
    return keys


def _plan_webp_decode(frames, cw, ch):
    """작업 목록 [(시작, 끝, 방식)] 을 프레임 순서대로 만든다. 구간은 키프레임에서만
    끊으므로 서로 독립이다.
    'still': 전체 캔버스를 덮는 알파 없는 키프레임 하나 -> 단독 webp 로 잘라 cv2 로 푼다.
    'anim' : 그 밖의 구간 -> 구간만 담은 작은 webp 를 만들어 Pillow 로 순서대로 푼다."""
    keys = _webp_keyframe_flags(frames, cw, ch)
    n = len(frames)
    items = []
    s = 0
    for i in range(1, n + 1):
        if i == n or keys[i]:
            f = frames[s]
            standalone = (i - s == 1 and f['w'] == cw and f['h'] == ch
                          and f['x'] == 0 and f['y'] == 0 and not f['alpha'])
            items.append((s, i, 'still' if standalone else 'anim'))
            s = i
    return items


def _build_webp_still(data, frame, cw, ch):
    """ANMF 프레임 하나의 이미지 데이터를 단독 webp 파일 바이트로 만든다."""
    a, b = frame['inner']
    flags = 0x10 if frame['alpha'] else 0
    vp8x = (b'VP8X' + struct.pack('<I', 10) + bytes((flags, 0, 0, 0))
            + (cw - 1).to_bytes(3, 'little') + (ch - 1).to_bytes(3, 'little'))
    body = b'WEBP' + vp8x + data[a:b]
    return b'RIFF' + struct.pack('<I', len(body)) + body


def _build_webp_segment(data, vp8x, anim, frames, s, e):
    """frames[s:e] 만 담은 독립적인 애니메이션 webp 바이트."""
    body = b'WEBP' + vp8x + anim + b''.join(
        data[f['start']:f['start'] + f['length']] for f in frames[s:e])
    return b'RIFF' + struct.pack('<I', len(body)) + body


def _apply_anim_adjustments_raw(raw, w, h, saturation, brightness, contrast):
    """decode_webp_animation_frames 와 같은 색 보정 (cv2, 없으면 Pillow)."""
    if saturation == 100 and brightness == 100 and contrast == 100:
        return raw, w, h
    result = _process_animated_frame_fast(raw, w, h, saturation, brightness, contrast, None, None)
    if result is not None:
        return result
    Image = get_pil_image()
    frame = Image.frombytes('RGBA', (w, h), raw)
    alpha = frame.getchannel('A')
    rgb = apply_color_adjustments(frame.convert('RGB'), saturation, brightness, contrast)
    out = rgb.convert('RGBA')
    out.putalpha(alpha)
    return out.tobytes('raw', 'RGBA'), rgb.width, rgb.height


class _WebpDecodePlan:
    """한 webp 파일을 어떻게 나눠서 풀지 (items) 와, 작업 하나를 푸는 방법 (decode_item).
    decode_item 은 여러 스레드에서 동시에 불러도 된다."""

    def __init__(self, data, info, items, target_w, target_h, saturation, brightness, contrast):
        self.data = data
        self.vp8x = info['vp8x']
        self.anim = info['anim']
        self.cw, self.ch = info['canvas']
        self.frames = info['frames']
        self.items = items
        self.target_w = target_w
        self.target_h = target_h
        self.saturation = saturation
        self.brightness = brightness
        self.contrast = contrast
        self.cv2_ok = True

    @staticmethod
    def build(source, frame_count, target_w, target_h, saturation, brightness, contrast):
        """source: 바이트(zip 항목) 또는 파일 경로. 지원하지 않는 구조면 None."""
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        elif isinstance(source, BytesIO):
            data = source.getvalue()
        else:
            with open(source, 'rb') as fp:
                data = fp.read()
        info = parse_animated_webp(data)
        if not info or len(info['frames']) != frame_count:
            return None
        cw, ch = info['canvas']
        items = _plan_webp_decode(info['frames'], cw, ch)
        return _WebpDecodePlan(data, info, items, target_w, target_h, saturation, brightness, contrast)

    def _decode_still(self, index):
        cv2 = get_cv2()
        np = get_numpy()
        still = _build_webp_still(self.data, self.frames[index], self.cw, self.ch)
        arr = cv2.imdecode(np.frombuffer(still, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if arr is None or arr.ndim != 3 or arr.dtype != np.uint8:
            raise ValueError('cv2 webp 디코딩 실패')
        h, w = arr.shape[:2]
        if (w, h) != (self.target_w, self.target_h):
            shrink = self.target_w < w or self.target_h < h
            arr = cv2.resize(arr, (self.target_w, self.target_h),
                             interpolation=cv2.INTER_AREA if shrink else cv2.INTER_LINEAR)
        channels = arr.shape[2]
        if channels == 3:
            code = cv2.COLOR_BGR2RGBA
        elif channels == 4:
            code = cv2.COLOR_BGRA2RGBA
        else:
            raise ValueError('예상하지 못한 채널 수')
        return cv2.cvtColor(arr, code).tobytes(), self.target_w, self.target_h

    def _decode_segment_pillow(self, s, e, emit, is_cancelled):
        Image = get_pil_image()
        mini = _build_webp_segment(self.data, self.vp8x, self.anim, self.frames, s, e)
        with Image.open(BytesIO(mini)) as im:
            for k in range(e - s):
                if is_cancelled():
                    raise _DecodeCancelled()
                im.seek(k)
                frame = im.convert('RGBA')
                if frame.size != (self.target_w, self.target_h):
                    frame = frame.resize((self.target_w, self.target_h), Image.BILINEAR)
                w, h = frame.size
                raw = frame.tobytes('raw', 'RGBA')
                raw, w, h = _apply_anim_adjustments_raw(
                    raw, w, h, self.saturation, self.brightness, self.contrast)
                emit(s + k, raw, w, h, False)

    def decode_item(self, item, emit, is_cancelled):
        """작업 하나를 풀어서 프레임마다 emit(index, raw_rgba, w, h, opaque) 을 부른다.
        opaque 는 알파가 전부 255 임이 확실할 때만 True."""
        s, e, mode = item
        if mode == 'still' and self.cv2_ok:
            decoded = None
            try:
                decoded = self._decode_still(s)
            except ImportError:
                self.cv2_ok = False
            except Exception:
                decoded = None
            if decoded is not None:
                raw, w, h = _apply_anim_adjustments_raw(
                    decoded[0], decoded[1], decoded[2],
                    self.saturation, self.brightness, self.contrast)
                emit(s, raw, w, h, True)
                return
        self._decode_segment_pillow(s, e, emit, is_cancelled)


def first_loop_can_start(total, decoded, elapsed, cum_s, workers, safety=1.25, margin=0.08):
    """지금 재생을 시작해도 남은 프레임이 제때 나오는지 (= 끊김 없이 첫 루프를 재생할
    수 있는지) 지금까지의 디코딩 속도로 어림한다.

    total: 전체 프레임 수, decoded: 지금까지 디코딩이 끝난 프레임 수, elapsed: 디코딩을
    시작한 뒤 지난 시간(초), cum_s[i]: 지금 재생을 시작하면 i 번째 프레임이 필요해지는
    시각(초) = 앞 프레임들의 길이 합."""
    if decoded >= total:
        return True
    # 속도를 믿을 만큼 측정되기 전에는 판단하지 않는다.
    if elapsed < 0.3 or decoded < min(total, max(2, workers)):
        return False
    rate = decoded / elapsed
    for i in range(decoded, total):
        if (i + 1 - decoded) / rate * safety + margin > cum_s[i]:
            return False
    return True


class FirstLoopJob:
    """애니메이션 하나의 루프 전체를 작업 스레드 여러 개로 푸는 작업 (GUI 스레드 밖).
    결과 프레임은 results 큐로 나오고, ImageViewer._first_loop_pump 가 GUI 스레드에서
    QPixmap 으로 바꿔 animated_frame_cache 에 넣는다."""

    def __init__(self, generation, source, frame_count, target_w, target_h,
                 saturation, brightness, contrast, delays_ms):
        self.generation = generation
        self.source = source
        self.frame_count = frame_count
        self.target_w = target_w
        self.target_h = target_h
        self.saturation = saturation
        self.brightness = brightness
        self.contrast = contrast
        self.settings_sig = (saturation, brightness, contrast)
        self.cum_s = []
        t = 0.0
        for d in delays_ms:
            self.cum_s.append(t)
            t += d / 1000.0
        self.results = queue.SimpleQueue()
        self.plan = None
        self.cancelled = False
        self.error = None
        self.t0 = time.perf_counter()
        # 앞에서부터 연속으로 QPixmap 변환까지 끝난 프레임 수. 작업 스레드가 읽어서,
        # GUI 스레드가 변환을 못 따라갈 때 원본 바이트가 메모리에 끝없이 쌓이지 않게 한다.
        self.prefix = 0
        self.window = max(_FIRST_LOOP_WORKER_COUNT * 3, 12)
        self.decoded = 0
        self.converted = 0
        self._converted_idx = set()
        self.first_shown = False
        self.playing = False
        self._lock = threading.Lock()

    def start(self, executor):
        executor.submit(self._prepare_and_submit, executor)

    def cancel(self):
        self.cancelled = True

    def release(self):
        """끝났거나 취소된 뒤 메모리 (파일 바이트, 큐에 남은 프레임) 를 돌려준다."""
        self.plan = None
        self.source = None
        while True:
            try:
                self.results.get_nowait()
            except queue.Empty:
                break

    def mark_converted(self, idx):
        self.converted += 1
        self._converted_idx.add(idx)
        while self.prefix in self._converted_idx:
            self._converted_idx.discard(self.prefix)
            self.prefix += 1

    def _prepare_and_submit(self, executor):
        try:
            if self.cancelled:
                return
            _set_current_thread_below_normal_priority()
            plan = _WebpDecodePlan.build(self.source, self.frame_count, self.target_w, self.target_h,
                                         self.saturation, self.brightness, self.contrast)
            if plan is None:
                self.error = 'webp 프레임 구조를 읽지 못했거나 프레임 수가 맞지 않음'
                return
            self.plan = plan
            for item in plan.items:
                if self.cancelled:
                    return
                executor.submit(self._run_item, item)
        except Exception as e:
            if self.error is None:
                self.error = f'{type(e).__name__}: {e}'

    def _run_item(self, item):
        plan = self.plan
        if plan is None or self.cancelled or self.error:
            return
        _set_current_thread_below_normal_priority()

        def emit(idx, raw, w, h, opaque):
            while not self.cancelled and idx - self.prefix >= self.window:
                time.sleep(0.004)
            if self.cancelled:
                raise _DecodeCancelled()
            with self._lock:
                self.decoded += 1
            self.results.put((idx, raw, w, h, opaque))

        try:
            plan.decode_item(item, emit, lambda: self.cancelled)
        except _DecodeCancelled:
            return
        except Exception as e:
            if self.error is None:
                self.error = f'{type(e).__name__}: {e}'

# ---------------------------------------------------------------------------
# --noconsole 로 빌드한 exe 용 로그 파일
# '실제 크기 / 창 크기' 토글을 이 시간(초) 안에 또 받으면 같은 입력의 중복으로 보고 무시한다.
# 무시된 시도도 "마지막 시도"로 쳐서, 이어지는 입력 (틸트 휠을 한 번 기울였을 때 여러 개로 오는 이벤트,
# 키 반복) 은 처음 하나만 동작한다. 정말 따로 누른 입력은 이 간격보다 길게 떨어져 있다.
TOGGLE_ACTUAL_SIZE_MIN_INTERVAL_S = 0.30


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
                # waitForReadyRead above runs its own nested event loop;
                # anything the callback scheduled (repaints, etc.) is
                # meant to run on the normal event loop once this slot
                # returns, but flushing it explicitly here removes any
                # dependency on exactly how Qt schedules that after a
                # reentrant wait like this one.
                QApplication.processEvents()
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
        # Written to a side file and swapped in, so a crash or power loss in
        # the middle of a write can't leave a half-written settings file:
        # load() falls back to the defaults for an unreadable file, and the
        # next save would then overwrite the user's real settings with them.
        tmp = self.settings_file + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            return
        try:
            os.replace(tmp, self.settings_file)
        except OSError:
            # e.g. the target is briefly locked: overwrite it in place instead.
            try:
                with open(self.settings_file, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            try:
                os.remove(tmp)
            except OSError:
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
# Kept small (1-4) on purpose: only one frame is ever "live" per movie plus
# anim_lookahead frames ahead of it (see ImageViewer.anim_lookahead), so
# more workers than that wouldn't speed up a single animation, they'd just
# take workers away from the shared pool.
_ANIM_WORKER_COUNT = max(1, min(4, (os.cpu_count() or 4) // 2))

# For _preload_neighbor_animations: fully decoding a whole animated-webp
# loop ahead of time (hundreds of frames) is heavy, sustained CPU work --
# very different from the one-frame-at-a-time jobs _anim_executor above is
# sized for. Its own pool, capped at 1, so it can never take a worker away
# from either that pool (which the currently-*playing* animation depends
# on for smooth playback) or the static-image pool above (which the
# image actually on screen depends on). One slot is enough: only the
# nearest not-yet-cached eligible neighbor is ever being prepared at a
# time (see ImageViewer._preload_neighbor_animations), the rest wait their
# turn as navigation continues or a slot frees up.
_ANIM_PRELOAD_WORKER_COUNT = 1

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
                          '.bmp', '.tif', '.tiff', '.ico',
                          # Added on request ("확장자 늘려줘, 속도 지장 없는 범위
                          # 에서"): all of these decode through the exact same
                          # QImageReader/Pillow paths already used above, so
                          # there's no new decode machinery and no speed
                          # impact -- confirmed each is actually registered
                          # in Pillow (not just assumed) before adding.
                          # .jfif is plain JPEG data under a different
                          # extension. .heic/.heif were left out: Pillow
                          # doesn't read them without an extra plugin
                          # (pillow-heif) that isn't confirmed installed
                          # here, and listing them as supported when they'd
                          # just fail to decode would be worse than not
                          # listing them.
                          '.jfif', '.tga', '.dds',
                          '.pbm', '.pgm', '.ppm', '.pnm',
                          '.avif', '.avifs'}
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=_DECODE_WORKER_COUNT)
    _anim_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_WORKER_COUNT)
    _anim_preload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_PRELOAD_WORKER_COUNT)
    # 고해상도 애니메이션 webp 의 첫 루프 병렬 디코딩 전용 (FirstLoopJob). 재생 중인
    # 애니메이션 하나를 위한 일이라 다른 풀과 섞지 않는다.
    _first_loop_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_FIRST_LOOP_WORKER_COUNT)

    @staticmethod
    def is_supported(filename):
        return os.path.splitext(filename)[1].lower() in ImageLoader.SUPPORTED_FORMATS

    @staticmethod
    def load_image_data(filepath, saturation=100, brightness=100, contrast=100, max_size=None):
        try:
            # WebP-only, no-adjustment-only fast path. Real logs showed
            # WebP decode through QImageReader.setScaledSize() below
            # running 100-300ms per image even at defaults -- confirmed
            # directly (not just inferred) that neither Pillow's draft()
            # nor cv2's IMREAD_REDUCED_COLOR_* flags give WebP the kind
            # of efficient reduced-scale decode JPEG gets from either
            # library, so there's no cheap-scaled-decode shortcut for
            # this format available anywhere in this codebase's toolbox.
            # cv2's plain full decode was still consistently ~30% faster
            # than Pillow's or Qt's for WebP in direct testing, though,
            # so worth taking when it applies.
            # Scoped to no-adjustment only because that's the specific
            # case real logs showed as slow; the adjustment branch below
            # already goes through Pillow regardless of decoder here, so
            # this wouldn't help it anyway.
            # EXIF safety: only taken when the file has no orientation
            # tag (or orientation 1/normal) -- checked via a cheap,
            # header-only Pillow open (no pixel decode), not by trying to
            # replicate Pillow's/Qt's rotation handling in cv2 by hand.
            # Anything else (a real rotation tag present) falls through
            # to the QImageReader path below, which already handles EXIF
            # correctly via setAutoTransform(True). Any failure at all
            # here (cv2 missing, decode error, unexpected channel count)
            # falls through the same way.
            # cv2 is only used once the background import (request_cv2_warmup)
            # has finished: importing it right here, on the first webp, would
            # cost more than this path saves on one image, so until then
            # images just go through the Qt reader below.
            if _cv2_module is None and filepath.lower().endswith('.webp'):
                request_cv2_warmup()
            if (saturation == 100 and brightness == 100 and contrast == 100
                    and filepath.lower().endswith('.webp') and _cv2_module is not None):
                try:
                    Image = get_pil_image()
                    with Image.open(filepath) as probe:
                        exif = probe.getexif()
                        orientation = exif.get(274, 1) if exif else 1
                    if orientation in (1, None):
                        cv2 = get_cv2()
                        if cv2 is not None:
                            raw = cv2.imread(filepath, cv2.IMREAD_UNCHANGED)
                            if raw is not None and raw.ndim == 3 and raw.shape[2] in (3, 4):
                                h0, w0 = raw.shape[:2]
                                if max_size and max_size[0] > 0 and max_size[1] > 0 and (w0 > max_size[0] or h0 > max_size[1]):
                                    scale = min(max_size[0] / w0, max_size[1] / h0)
                                    raw = cv2.resize(raw, (max(1, round(w0 * scale)), max(1, round(h0 * scale))),
                                                      interpolation=cv2.INTER_AREA)
                                h1, w1 = raw.shape[:2]
                                if raw.shape[2] == 4:
                                    rgba = cv2.cvtColor(raw, cv2.COLOR_BGRA2RGBA)
                                    return QImage(rgba.tobytes(), w1, h1, w1 * 4, QImage.Format_RGBA8888).copy()
                                rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
                                return QImage(rgb.tobytes(), w1, h1, w1 * 3, QImage.Format_RGB888).copy()
                except Exception:
                    pass  # fall through to the QImageReader path below

            # Decode via Qt first regardless of whether an adjustment is
            # active. QImageReader.setScaledSize() gets an efficient
            # reduced-resolution decode for whatever formats its plugins
            # support scaled reading for -- not just JPEG. The previous
            # version only took this path when saturation/brightness/
            # contrast were all 100 and fell back to a full Pillow decode
            # otherwise; Image.draft() recovered some of that for JPEG,
            # but draft() is a no-op for WebP/PNG/etc, so a static WebP
            # with any adjustment on was still fully decoded at native
            # resolution on every navigation -- exactly why adjusted
            # navigation stayed slow for WebP specifically even after the
            # draft() fix helped JPEG. Decoding through Qt unconditionally
            # and only reaching into Pillow for the color math itself (on
            # the already-small result) fixes that for every format
            # uniformly, and is the same RGBA8888/bits()/byteCount()
            # QImage<->Pillow handoff already used for animated frames
            # elsewhere in this file.
            reader = QImageReader(filepath)
            reader.setAutoTransform(True)
            if max_size and max_size[0] > 0 and max_size[1] > 0:
                src_size = reader.size()
                if src_size.isValid() and src_size.width() > 0 and src_size.height() > 0:
                    reader.setScaledSize(src_size.scaled(
                        QSize(int(max_size[0]), int(max_size[1])), Qt.KeepAspectRatio))
            image = reader.read()

            if saturation == 100 and brightness == 100 and contrast == 100:
                if not image.isNull():
                    return image
            elif not image.isNull():
                Image = get_pil_image()
                rgba = image.convertToFormat(QImage.Format_RGBA8888)
                w, h = rgba.width(), rgba.height()
                ptr = rgba.bits()
                ptr.setsize(rgba.byteCount())
                pil_rgba = Image.frombuffer('RGBA', (w, h), bytes(ptr), 'raw', 'RGBA', 0, 1)
                # Keep the source alpha untouched through the color math --
                # apply_color_adjustments only takes/returns RGB, and a
                # transparent pixel's underlying RGB value is often
                # undefined/black once alpha is dropped, so a naive
                # convert('RGB') here would bake that black in permanently.
                # Splitting alpha out and re-attaching it after keeps
                # genuinely transparent areas transparent.
                alpha = pil_rgba.getchannel('A')
                pil_rgb = apply_color_adjustments(pil_rgba.convert('RGB'), saturation, brightness, contrast)
                pil_out = pil_rgb.convert('RGBA')
                pil_out.putalpha(alpha)
                data = pil_out.tobytes('raw', 'RGBA')
                return QImage(data, pil_out.width, pil_out.height, pil_out.width * 4, QImage.Format_RGBA8888).copy()

            # Pillow fallback -- only reached if Qt couldn't decode this
            # file at all (exotic format/corruption).
            Image = get_pil_image()
            with Image.open(filepath) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                has_alpha = src.mode in ('RGBA', 'LA', 'PA') or (src.mode == 'P' and 'transparency' in src.info)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    try:
                        src.draft('RGB', max_size)
                    except Exception:
                        pass
                img = src.convert('RGBA') if has_alpha else src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    # BILINEAR here trades a little resample quality for real
                    # speed: this thumbnail gets scaled again by Qt to the
                    # exact viewport size right after (update_image_display),
                    # so LANCZOS's extra sharpness on this intermediate step
                    # was mostly being thrown away anyway.
                    resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                    img.thumbnail(max_size, resample)
                if saturation != 100 or brightness != 100 or contrast != 100:
                    if has_alpha:
                        alpha = img.getchannel('A')
                        img = apply_color_adjustments(img.convert('RGB'), saturation, brightness, contrast).convert('RGBA')
                        img.putalpha(alpha)
                    else:
                        img = apply_color_adjustments(img, saturation, brightness, contrast)
                if has_alpha:
                    data = img.tobytes('raw', 'RGBA')
                    return QImage(data, img.width, img.height, img.width * 4, QImage.Format_RGBA8888).copy()
                data = img.tobytes('raw', 'RGB')
                return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        except Exception as e:
            print(f"이미지 백그라운드 로드 오류: {e}")
        return None

    @staticmethod
    def load_thumbnail(filepath, size=(150, 150)):
        image = ImageLoader.load_image_data(filepath, max_size=size)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        return None

    @classmethod
    def shutdown_executor(cls):
        cls._shutdown = True
        for name in ('_executor', '_anim_executor', '_anim_preload_executor', '_first_loop_executor'):
            pool = getattr(cls, name)
            # wait=False: queued jobs are dropped, a job that is already
            # running finishes on its own -- closing the window must not
            # block on a long decode.
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)
            except Exception:
                pass

    @classmethod
    def restart_executor(cls):
        if cls._shutdown:
            cls._executor = concurrent.futures.ThreadPoolExecutor(max_workers=_DECODE_WORKER_COUNT)
            cls._anim_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_WORKER_COUNT)
            cls._anim_preload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_ANIM_PRELOAD_WORKER_COUNT)
            cls._first_loop_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_FIRST_LOOP_WORKER_COUNT)
            cls._shutdown = False


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
        import zipfile
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for info in zf.infolist():
                    if not info.is_dir() and os.path.splitext(info.filename)[1].lower() in ZipHandler._supported:
                        images.append(info.filename)
        except Exception as e:
            print(f"ZIP 목록 로드 오류: {e}")
        images.sort(key=natural_sort_key)
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
            import zipfile
            zf = zipfile.ZipFile(key, 'r')
            handles[key] = zf
        return zf

    @staticmethod
    def load_image_data(zip_path, filename, saturation=100, brightness=100, contrast=100, max_size=None):
        try:
            zf = ZipHandler._get_zip(zip_path)
            with zf.open(filename, 'r') as fp:
                data = fp.read()

            # See the matching comment in ImageLoader.load_image_data --
            # same WebP-only, no-adjustment-only, EXIF-safe cv2 fast path.
            if _cv2_module is None and filename.lower().endswith('.webp'):
                request_cv2_warmup()
            if (saturation == 100 and brightness == 100 and contrast == 100
                    and filename.lower().endswith('.webp') and _cv2_module is not None):
                try:
                    Image = get_pil_image()
                    with Image.open(BytesIO(data)) as probe:
                        exif = probe.getexif()
                        orientation = exif.get(274, 1) if exif else 1
                    if orientation in (1, None):
                        cv2 = get_cv2()
                        if cv2 is not None:
                            np = get_numpy()
                            np_arr = np.frombuffer(data, dtype=np.uint8)
                            raw = cv2.imdecode(np_arr, cv2.IMREAD_UNCHANGED)
                            if raw is not None and raw.ndim == 3 and raw.shape[2] in (3, 4):
                                h0, w0 = raw.shape[:2]
                                if max_size and max_size[0] > 0 and max_size[1] > 0 and (w0 > max_size[0] or h0 > max_size[1]):
                                    scale = min(max_size[0] / w0, max_size[1] / h0)
                                    raw = cv2.resize(raw, (max(1, round(w0 * scale)), max(1, round(h0 * scale))),
                                                      interpolation=cv2.INTER_AREA)
                                h1, w1 = raw.shape[:2]
                                if raw.shape[2] == 4:
                                    rgba = cv2.cvtColor(raw, cv2.COLOR_BGRA2RGBA)
                                    return QImage(rgba.tobytes(), w1, h1, w1 * 4, QImage.Format_RGBA8888).copy()
                                rgb = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
                                return QImage(rgb.tobytes(), w1, h1, w1 * 3, QImage.Format_RGB888).copy()
                except Exception:
                    pass  # fall through to the QImageReader path below

            # See the matching comment in ImageLoader.load_image_data --
            # decode via Qt first regardless of adjustments, so every
            # format gets an efficient reduced-resolution decode, not
            # just JPEG (which is all Pillow's draft() ever covered).
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

            if saturation == 100 and brightness == 100 and contrast == 100:
                if not image.isNull():
                    return image
            elif not image.isNull():
                Image = get_pil_image()
                rgba = image.convertToFormat(QImage.Format_RGBA8888)
                w, h = rgba.width(), rgba.height()
                ptr = rgba.bits()
                ptr.setsize(rgba.byteCount())
                pil_rgba = Image.frombuffer('RGBA', (w, h), bytes(ptr), 'raw', 'RGBA', 0, 1)
                # See the matching comment in ImageLoader.load_image_data --
                # keep the source alpha untouched through the color math
                # instead of letting convert('RGB') bake in whatever
                # undefined color a transparent pixel happens to store.
                alpha = pil_rgba.getchannel('A')
                pil_rgb = apply_color_adjustments(pil_rgba.convert('RGB'), saturation, brightness, contrast)
                pil_out = pil_rgb.convert('RGBA')
                pil_out.putalpha(alpha)
                raw = pil_out.tobytes('raw', 'RGBA')
                return QImage(raw, pil_out.width, pil_out.height, pil_out.width * 4, QImage.Format_RGBA8888).copy()

            # Pillow fallback -- only reached if Qt couldn't decode this
            # entry at all.
            Image = get_pil_image()
            with Image.open(BytesIO(data)) as src:
                if getattr(src, 'is_animated', False):
                    src.seek(0)
                has_alpha = src.mode in ('RGBA', 'LA', 'PA') or (src.mode == 'P' and 'transparency' in src.info)
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    try:
                        src.draft('RGB', max_size)
                    except Exception:
                        pass
                img = src.convert('RGBA') if has_alpha else src.convert('RGB')
                if max_size and max_size[0] > 0 and max_size[1] > 0:
                    resample = Image.Resampling.BILINEAR if hasattr(Image, 'Resampling') else Image.BILINEAR
                    img.thumbnail(max_size, resample)
                if saturation != 100 or brightness != 100 or contrast != 100:
                    if has_alpha:
                        alpha = img.getchannel('A')
                        img = apply_color_adjustments(img.convert('RGB'), saturation, brightness, contrast).convert('RGBA')
                        img.putalpha(alpha)
                    else:
                        img = apply_color_adjustments(img, saturation, brightness, contrast)
                if has_alpha:
                    raw = img.tobytes('raw', 'RGBA')
                    return QImage(raw, img.width, img.height, img.width * 4, QImage.Format_RGBA8888).copy()
                raw = img.tobytes('raw', 'RGB')
                return QImage(raw, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        except Exception as e:
            print(f"ZIP 이미지 백그라운드 로드 오류: {e}")
        return None

    @staticmethod
    def load_image_from_zip(zip_path, filename, saturation=100, brightness=100, contrast=100, max_size=None):
        image = ZipHandler.load_image_data(zip_path, filename, saturation, brightness, contrast, max_size)
        if image and not image.isNull():
            return QPixmap.fromImage(image)
        return None

    @staticmethod
    def load_thumbnail(zip_path, filename, size=(150, 150)):
        return ZipHandler.load_image_from_zip(zip_path, filename, max_size=size)


class ImageLoadBridge(QObject):
    loaded = pyqtSignal(int, str, object, bool)
    animated_frame = pyqtSignal(int, int, object)
    hq_resample = pyqtSignal(int, object)
    # (source_key, (filename, result_or_None)) -- see
    # ImageViewer._submit_anim_preload/_on_anim_preload_ready. Deliberately
    # not gated by current_movie_generation like animated_frame above: the
    # whole point is to finish for a file that ISN'T the one on screen.
    anim_preload_ready = pyqtSignal(object, object)

class ThumbnailLoadBridge(QObject):
    loaded = pyqtSignal(int, object)

class ImageListDialog(QDialog):
    # Windows 11 Explorer's "Large icons" folder view renders thumbnails
    # at 96x96 -- confirmed (not guessed) via web search, since getting
    # this specific number wrong would be obviously off next to the real
    # thing.
    THUMB_SIZE = 96

    def __init__(self, image_list, current_index, parent=None, current_zip=None):
        super().__init__(parent)
        self.image_list = image_list
        self.current_index = current_index
        self.selected_index = current_index
        self.current_zip = current_zip
        # Settings live on the main window (self.parent()), not this
        # dialog -- same object _restore_geometry reads from and done()
        # writes to below, so window position/size + splitter position
        # persist across dialog opens the same way the main window's own
        # geometry already does (see ImageViewer.save_settings).
        self._settings = getattr(parent, 'settings', None)
        self.thumb_bridge = ThumbnailLoadBridge()
        self.thumb_bridge.loaded.connect(self._on_thumbnail_loaded)
        self.thumb_generation = 0
        # The full-size decode behind the current preview -- see
        # show_preview/_rescale_preview. Kept around so resizing the
        # preview pane (drag the splitter, or resize the whole dialog)
        # only needs a cheap QPixmap.scaled() call, not a fresh decode.
        self._preview_pixmap = None
        self.init_ui()
        self._load_list_thumbnails()

    def init_ui(self):
        self.setWindowTitle('이미지 목록')
        self.setModal(True)
        self.setMinimumSize(280, 320)
        self.setStyleSheet("""
            QDialog { background-color: #2b2b2b; color: white; }
            QListWidget { background-color: #3c3c3c; color: white; border: 1px solid #555; }
            QListWidget::item { padding: 4px; border-radius: 4px; }
            QListWidget::item:selected { background-color: #4a90d9; }
            QLabel { color: white; }
            QPushButton { background-color: #3c3c3c; color: white; border: 1px solid #555; padding: 5px; }
            QPushButton:hover { background-color: #4c4c4c; }
            QSplitter::handle { background-color: #555; }
            QSplitter::handle:vertical { height: 4px; }
        """)
        layout = QVBoxLayout(self)

        # A splitter (not a plain stacked layout) so the preview pane and
        # the file list can each be resized independently by dragging the
        # handle between them, on request -- sizes are saved/restored in
        # done()/_restore_geometry below, same as the window size.
        self.splitter = QSplitter(Qt.Vertical)

        self.preview_label = QLabel('이미지를 선택하세요')
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(60)
        self.preview_label.setStyleSheet("border: 1px solid #555; background-color: #3c3c3c;")
        self.splitter.addWidget(self.preview_label)

        self.splitter.splitterMoved.connect(lambda pos, index: self._rescale_preview())

        self.list_widget = QListWidget()
        # Grid-of-thumbnails instead of a plain text list -- see
        # _load_list_thumbnails for how each icon actually gets filled in.
        self.list_widget.setViewMode(QListView.IconMode)
        self.list_widget.setIconSize(QSize(self.THUMB_SIZE, self.THUMB_SIZE))
        self.list_widget.setResizeMode(QListView.Adjust)
        self.list_widget.setMovement(QListView.Static)
        self.list_widget.setWordWrap(True)
        self.list_widget.setSpacing(8)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setMinimumHeight(60)
        placeholder = self._placeholder_icon()
        for i, image_path in enumerate(self.image_list):
            display_name = os.path.basename(image_path)
            item = QListWidgetItem(placeholder, display_name)
            item.setData(Qt.UserRole, i)
            item.setTextAlignment(Qt.AlignHCenter)
            self.list_widget.addItem(item)
        self.list_widget.setCurrentRow(self.current_index)
        self.list_widget.itemDoubleClicked.connect(self.on_double_click)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.splitter.addWidget(self.list_widget)

        layout.addWidget(self.splitter)
        self.show_preview(self.current_index)

        button_layout = QHBoxLayout()
        select_button = QPushButton('선택')
        select_button.clicked.connect(self.accept)
        cancel_button = QPushButton('취소')
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(select_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)

        self._restore_geometry()
        current_item = self.list_widget.item(self.current_index)
        if current_item is not None:
            QTimer.singleShot(0, lambda: self.list_widget.scrollToItem(
                current_item, QListWidget.PositionAtCenter))
        # By the time resize/splitter events fire from _restore_geometry
        # above, the splitter may not have its restored proportions
        # applied yet (setSizes happens after resize() in there), so the
        # preview could get scaled once against a not-yet-final size.
        # Harmless, but this deferred call -- running once everything
        # from _restore_geometry has actually settled -- guarantees the
        # preview ends up matching the real, final layout regardless of
        # that ordering.
        QTimer.singleShot(0, self._rescale_preview)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Cheap (scales an already-decoded pixmap, no re-decode), so no
        # debounce needed here -- unlike the thumbnail grid, which used
        # to also resize with the dialog and needed one because it had
        # to re-decode every thumbnail at the new size. That turned out
        # to not be what was actually wanted (the grid stays at a fixed
        # Windows-11-large-icons size now); this preview rescale is what
        # "resize with the window" meant instead.
        self._rescale_preview()

    def _rescale_preview(self):
        if not self._preview_pixmap:
            return
        target = self.preview_label.size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._preview_pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_label.setPixmap(scaled)

    def _placeholder_icon(self):
        pm = QPixmap(self.THUMB_SIZE, self.THUMB_SIZE)
        pm.fill(QColor('#4c4c4c'))
        return QIcon(pm)

    def _restore_geometry(self):
        geom = self._settings.get('image_list_dialog_geometry') if self._settings else None
        width = geom.get('width') if geom else None
        height = geom.get('height') if geom else None
        if width and height:
            self.resize(width, height)
        else:
            self.resize(420, 620)
        x = geom.get('x') if geom else None
        y = geom.get('y') if geom else None
        if x is not None and y is not None:
            self.move(x, y)
        sizes = geom.get('splitter_sizes') if geom else None
        if sizes and len(sizes) == 2 and all(isinstance(s, int) and s > 0 for s in sizes):
            self.splitter.setSizes(sizes)
        else:
            self.splitter.setSizes([200, 380])

    def done(self, r):
        # Overridden instead of closeEvent: QDialog.accept()/reject() --
        # what the 선택/취소 buttons and double-clicking an item all
        # call -- go through done() and then hide(), never through
        # close(), so a closeEvent override never actually ran for any
        # of this dialog's normal dismissal paths (only for the window's
        # own X button, via Qt's default closeEvent->reject() handling).
        # done() is the one place all three routes funnel through.
        if self._settings:
            pos = self.pos()
            self._settings.set('image_list_dialog_geometry', {
                'x': pos.x(),
                'y': pos.y(),
                'width': self.width(),
                'height': self.height(),
                'splitter_sizes': self.splitter.sizes(),
            })
        super().done(r)

    def _load_list_thumbnails(self):
        # One background job per file, reusing the same shared decode
        # pool (and so the same efficient-scaled-decode/cv2 paths) the
        # main viewer uses for everything else -- see ImageLoader.
        # thumb_generation guards against a stale earlier batch's results
        # landing on the wrong items if this ever gets called a second
        # time while the dialog is still open (it currently isn't, but
        # cheap insurance). The try/except around emit below is the
        # actual guard against the dialog (and thumb_bridge with it)
        # having already been closed and garbage-collected by the time a
        # background job finishes -- closing the dialog doesn't cancel
        # jobs already submitted to the shared pool.
        self.thumb_generation += 1
        generation = self.thumb_generation
        current_zip = self.current_zip
        size = (self.THUMB_SIZE, self.THUMB_SIZE)
        for i, image_path in enumerate(self.image_list):
            def worker(path=image_path):
                try:
                    if current_zip:
                        return ZipHandler.load_thumbnail(current_zip, path, size=size)
                    return ImageLoader.load_thumbnail(path, size=size)
                except Exception:
                    return None
            future = ImageLoader._executor.submit(worker)
            def done(fut, idx=i, gen=generation):
                try:
                    pixmap = fut.result()
                except Exception:
                    pixmap = None
                try:
                    self.thumb_bridge.loaded.emit(idx, (gen, pixmap))
                except RuntimeError:
                    pass  # dialog already closed/destroyed
            future.add_done_callback(done)

    def _on_thumbnail_loaded(self, index, payload):
        generation, pixmap = payload
        if generation != self.thumb_generation:
            return
        if not pixmap or pixmap.isNull():
            return
        item = self.list_widget.item(index)
        if item is None:
            return
        item.setIcon(QIcon(pixmap))

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
            # Decoded once at a reasonably large fixed size; resizeEvent/
            # the splitter's splitterMoved above just rescale this same
            # pixmap afterward (cheap) instead of re-decoding from disk
            # every time the preview pane's size changes.
            preview_size = (800, 800)
            if self.current_zip:
                pixmap = ZipHandler.load_thumbnail(self.current_zip, self.image_list[index], size=preview_size)
            else:
                pixmap = ImageLoader.load_thumbnail(self.image_list[index], size=preview_size)
            if pixmap and not pixmap.isNull():
                self._preview_pixmap = pixmap
                self._rescale_preview()
            else:
                self._preview_pixmap = None
                self.preview_label.setText('미리보기 불가')
        except Exception:
            self._preview_pixmap = None
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

class FileAssociationDialog(QDialog):
    """One checkbox per supported extension (see ImageLoader.
    SUPPORTED_FORMATS); toggling a box immediately registers/
    unregisters that extension with this app in the Windows registry
    (see set_extension_association) rather than needing a separate
    save step -- matches how each checkbox's own state is read back
    from the registry (is_extension_associated), so what's on screen
    always reflects what's actually registered right now."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('파일 형식 연결')
        self.setModal(True)
        if parent is not None:
            self.setStyleSheet(parent.styleSheet())
        layout = QVBoxLayout(self)

        note = QLabel(
            '체크한 형식의 파일은 이 프로그램으로 열리도록 등록됩니다.\n'
            '체크 해제하면 이 프로그램이 등록했던 연결만 해제되며,\n'
            '다른 프로그램이 가진 연결은 건드리지 않습니다.'
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        exts = sorted(ImageLoader.SUPPORTED_FORMATS)
        self.checkboxes = {}
        columns = 4
        for i, ext in enumerate(exts):
            checkbox = QCheckBox(ext)
            checkbox.setChecked(is_extension_associated(ext))
            checkbox.toggled.connect(lambda checked, e=ext: self._on_toggled(e, checked))
            self.checkboxes[ext] = checkbox
            grid.addWidget(checkbox, i // columns, i % columns)
        layout.addWidget(grid_widget)

        close_button = QPushButton('닫기')
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

    def _on_toggled(self, ext, checked):
        ok = set_extension_association(ext, checked)
        if not ok:
            QMessageBox.warning(self, '파일 연결 실패', f'{ext} 연결 설정에 실패했습니다.')
            checkbox = self.checkboxes[ext]
            checkbox.blockSignals(True)
            checkbox.setChecked(not checked)
            checkbox.blockSignals(False)

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.init_ui()
        self.load_settings()
    
    def init_ui(self):
        self.setWindowTitle('설정')
        self.setModal(True)
        self.setMinimumWidth(420)
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
        # Two columns side by side instead of one long vertical stack, so
        # the dialog stays roughly as tall as before even with the added
        # file-association section -- see the addWidget calls below,
        # which go to left_column/right_column instead of layout directly
        # for everything except the Save/Cancel row at the very end.
        columns_layout = QHBoxLayout()
        left_column = QVBoxLayout()
        right_column = QVBoxLayout()
        columns_layout.addLayout(left_column)
        columns_layout.addLayout(right_column)
        layout.addLayout(columns_layout)
        
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
        left_column.addWidget(display_group)

        zip_group = QGroupBox('압축 파일')
        zip_layout = QFormLayout()
        self.remember_zip_position = QCheckBox('마지막으로 본 위치 기억 (최근 30개 파일)')
        zip_layout.addRow('', self.remember_zip_position)
        zip_note = QLabel('켜두면 압축 파일을 다시 열었을 때 마지막으로 보던 이미지부터 이어서 보여줍니다.')
        zip_note.setWordWrap(True)
        zip_layout.addRow('', zip_note)
        zip_group.setLayout(zip_layout)
        left_column.addWidget(zip_group)
        
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
        left_column.addWidget(static_adjust_group)

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
        left_column.addWidget(anim_adjust_group)

        apply_button = QPushButton('현재 이미지에 즉시 적용')
        apply_button.clicked.connect(self.apply_immediately)
        left_column.addWidget(apply_button)
        
        snap_group = QGroupBox('창 자석 기능')
        snap_layout = QFormLayout()
        self.snap_enabled = QCheckBox('화면 가장자리에 달라붙기')
        snap_layout.addRow('', self.snap_enabled)
        self.snap_threshold = QSpinBox()
        self.snap_threshold.setRange(5, 50)
        self.snap_threshold.setSuffix(' 픽셀')
        snap_layout.addRow('자석 작동 거리:', self.snap_threshold)
        snap_group.setLayout(snap_layout)
        right_column.addWidget(snap_group)
        
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
        right_column.addWidget(slideshow_group)

        file_assoc_group = QGroupBox('파일 연결')
        file_assoc_layout = QVBoxLayout()
        file_assoc_note = QLabel('특정 이미지 형식을 이 프로그램으로 열리도록 등록합니다.')
        file_assoc_note.setWordWrap(True)
        file_assoc_layout.addWidget(file_assoc_note)
        file_assoc_button = QPushButton('파일 형식 연결 설정...')
        file_assoc_button.clicked.connect(self.open_file_association_dialog)
        file_assoc_layout.addWidget(file_assoc_button)
        file_assoc_group.setLayout(file_assoc_layout)
        right_column.addWidget(file_assoc_group)
        
        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel('배경색:'))
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self.choose_color)
        color_layout.addWidget(self.color_button)
        right_column.addLayout(color_layout)
        
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
    
    def open_file_association_dialog(self):
        dialog = FileAssociationDialog(self)
        dialog.exec_()
    
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
        self.load_bridge.anim_preload_ready.connect(self._on_anim_preload_ready)
        self.load_generation = 0
        self.loading_keys = set()
        # Cache the expensive color-adjusted source separately from the
        # display-size cache. This prevents repeated Pillow work when navigating.
        self._adjusted_image_cache = {}
        self._adjusted_image_cache_order = []
        self._adjusted_image_cache_limit = 24
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
        # 2 -> 3: gives the first (uncached) loop of a demanding animation
        # a slightly deeper buffer to work with before playback can catch
        # up to an unprocessed frame -- paired with _ANIM_WORKER_COUNT's
        # matching bump above. This only changes how far ahead
        # _prefetch_ahead looks for work to hand the pool; it doesn't
        # change per-frame cost, so it helps most when a frame or two of
        # head start is what's missing, not when a single frame's own
        # processing time already exceeds the animation's frame interval.
        self.anim_lookahead = 3
        self.animated_inflight_keys = set()
        # Cached replay (see _try_start_anim_cache_playback): after the
        # first loop, QMovie is paused and this timer shows the cached
        # frames itself, using the per-frame durations in anim_frame_delays
        # (read from the file by read_webp_animation_info; None when the
        # current animation isn't eligible, e.g. a gif).
        self.anim_frame_delays = None
        self.anim_cache_playing = False
        self.anim_cache_index = -1
        self._anim_cache_deadline = 0.0
        self.anim_cache_timer = QTimer(self)
        self.anim_cache_timer.setSingleShot(True)
        self.anim_cache_timer.setTimerType(Qt.PreciseTimer)
        self.anim_cache_timer.timeout.connect(self._anim_cache_tick)
        # 고해상도 애니메이션 webp 의 첫 루프 병렬 디코딩 (FirstLoopJob) 상태와, 그 결과를
        # GUI 스레드에서 QPixmap 으로 바꿔 캐시에 넣는 타이머. _start_first_loop_decode 참고.
        self._first_loop_job = None
        self._first_loop_last_restart = 0.0
        # See toggle_actual_size: when the last toggle attempt happened (our own clock, and the
        # native input event's timestamp), and the timestamp of the input event being dispatched.
        self._last_toggle_attempt = -1e9
        self._last_toggle_attempt_ts = None
        self._input_ts_ms = None
        self._first_loop_timer = QTimer(self)
        self._first_loop_timer.setTimerType(Qt.PreciseTimer)
        self._first_loop_timer.timeout.connect(self._first_loop_pump)
        # Finished frame caches kept from animations navigated away from
        # (see _stash_animated_cache), least recently kept first, keyed by
        # _animation_source_key. current_movie_source is that key for the
        # animation on screen now.
        self.retained_anim_caches = OrderedDict()
        self.current_movie_source = None
        # filename -> threading.Event, for neighbor animations currently
        # being pre-decoded in the background (see
        # _preload_neighbor_animations/_submit_anim_preload). Sized 1 in
        # practice (_ANIM_PRELOAD_WORKER_COUNT), but keyed by name rather
        # than assumed-singular so cancelling stale ones stays simple.
        self.anim_preload_inflight = {}
        # _animation_source_key(...) results confirmed NOT eligible for
        # this feature (see worker()'s False returns below) -- skipped on
        # sight instead of re-running the same container-metadata checks
        # on every single navigation for as long as the file stays in
        # range. Keyed by the same (path, size, mtime) identity as
        # retained_anim_caches, so editing the file clears its entry
        # naturally. Bounded like the app's other small caches.
        self.anim_preload_ineligible = OrderedDict()
        # Source keys of neighbors that finished decoding but were NOT kept
        # (no room, or trimmed straight away). Skipped until the next
        # navigation instead of being decoded all over again -- with
        # nothing having changed, that would just repeat the same wasted
        # work forever. Cleared by show_current_image.
        self.anim_preload_declined = OrderedDict()
        # retained_anim_caches' size the last time _preload_neighbor_
        # animations_impl looked (see there): a file only gets a fresh
        # look after being declined for lack of room once this actually
        # DROPS, since that's the only thing that could free room for it.
        self._anim_declined_pool_size = 0
        # The finished decode currently being turned into pixmaps, a frame
        # per timer tick (see _begin_anim_admission), or None.
        self.anim_admit = None
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
        if get_icon_path():
            icon = get_app_icon()
            self.setWindowIcon(icon)
            app = QApplication.instance()
            if app:
                app.setWindowIcon(icon)

    def showEvent(self, event):
        super().showEvent(event)
        if get_icon_path():
            icon = get_app_icon()
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
        if not key_sequence:
            # A key Qt has no name for gives an empty string, and every
            # unassigned shortcut slot is an empty string too -- so such a key
            # used to "match" whichever action came first with a free slot
            # (even close_program, which is checked first).
            event.accept()
            return
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
                if action_name in ('toggle_actual_size', 'toggle_fullscreen'):
                    # Holding the key makes the OS repeat it; a toggle fired
                    # again and again just flips back and forth.
                    if event.isAutoRepeat():
                        event.accept()
                        return
                    try:
                        ts = event.timestamp()
                    except Exception:
                        ts = None
                    self._input_ts_ms = ts
                try:
                    callback()
                finally:
                    self._input_ts_ms = None
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
        values = {'zip_position_history': dict(self.zip_position_history)}
        if not self.isFullScreen():
            pos = self.pos()
            size = self.size()
            values['window_geometry'] = {
                'x': pos.x(),
                'y': pos.y(),
                'width': size.width(),
                'height': size.height()
            }
        # One write of the settings file instead of one per key.
        self.settings.update_many(values)
    
    def load_path(self, path):
        self.bring_to_front()
        if os.path.isdir(path):
            self.load_directory(path)
        elif os.path.isfile(path):
            if ZipHandler.is_zip(path):
                self.load_zip(path)
            else:
                self.load_single_file(path)
    
    def load_directory(self, directory, auto_show=True):
        self.load_generation += 1
        self.cache_manager.clear()
        self.image_list = []
        self.current_zip = None
        try:
            # Filter by extension first and sort only what is left: the
            # regex-based natural key used to be computed for every file in
            # the folder, images or not, which was the slowest part of
            # opening one image out of a big folder.
            names = [n for n in os.listdir(directory) if ImageLoader.is_supported(n)]
            names.sort(key=natural_sort_key)
            self.image_list = [os.path.join(directory, n) for n in names]
        except Exception:
            self.image_list = []
        if self.image_list:
            self.current_index = 0
            if auto_show:
                self.show_current_image()
        else:
            self.image_label.clear()

    def load_single_file(self, filepath):
        filepath = os.path.abspath(filepath)
        # auto_show=False: otherwise load_directory would decode and display
        # index 0 (alphabetically first in the folder) only to throw it away
        # once this file's real index is found below.
        self.load_directory(os.path.dirname(filepath), auto_show=False)
        try:
            # Every entry lives in this same folder, so comparing the file
            # names (case-insensitively, like the file system) is enough.
            target = os.path.normcase(os.path.basename(filepath))
            for i, img_path in enumerate(self.image_list):
                if os.path.normcase(os.path.basename(img_path)) == target:
                    self.current_index = i
                    break
            self.show_current_image()
        except Exception:
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
        # First, while everything about the animation being left is still in
        # place: may move its finished frame cache into retained_anim_caches
        # instead of having it cleared at the bottom.
        kept_cache = self._stash_animated_cache()
        self._cancel_first_loop_decode()
        self._stop_anim_cache_playback()
        self.anim_frame_delays = None
        self.current_movie_source = None
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
        if kept_cache:
            # The frames now belong to retained_anim_caches -- start a fresh
            # dict rather than clear() the one that entry holds.
            self.animated_frame_cache = OrderedDict()
        else:
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
        #
        # A fast/DIP-resolution preview tier used to exist here too, shown
        # during rapid navigation and upgraded to this sharp size once
        # navigation settled -- removed on request: the blurry-then-sharp
        # transition when it kicked in was more distracting than the
        # slower navigation it was trading for.
        # Small safety margin (also scaled) prevents repeated reloads
        # caused by tiny widget changes.
        return (max(64, int((size.width() + 64) * dpr)), max(64, int((size.height() + 64) * dpr)))

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
            if generation != self.load_generation:
                # Superseded by a later navigation before this job even
                # started running. With a worker pool of limited size,
                # rapid navigation (e.g. holding an arrow key, or flicking
                # quickly through static WebP files) can queue up more
                # decode jobs -- both "the image now on screen" and
                # _preload_neighbors' speculative ones -- than can finish
                # before the user has already moved past them. Bailing out
                # here before doing any real decode work lets a backlog of
                # now-irrelevant jobs drain almost instantly instead of
                # each one running its full decode first, which is what
                # let a big backlog visibly delay the image the user
                # actually landed on.
                self.loading_keys.discard(key)
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
        # Falls back to a plain gray box if decoding ever fails.
        if self._default_broken_pixmap_cache is None:
            import base64
            pixmap = QPixmap()
            try:
                ok = pixmap.loadFromData(base64.b64decode(_BROKEN_IMAGE_B64), 'PNG') and not pixmap.isNull()
            except Exception:
                ok = False
            if not ok:
                pixmap = QPixmap(400, 300)
                pixmap.fill(QColor('#3c3c3c'))
            self._default_broken_pixmap_cache = pixmap
        return self._default_broken_pixmap_cache

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
        self.update_image_display()
        # setPixmap() above only *schedules* a repaint for whenever Qt's
        # event loop next gets to it -- normally fine, but this can be
        # called from inside SingleApplication.on_new_connection's
        # socket.waitForReadyRead(), itself a nested/reentrant event loop
        # (see the comment there). A scheduled-but-not-yet-run repaint in
        # that context was what let a single image file opened from
        # outside the app (while it was already running) sit on screen
        # unchanged until something else -- a window resize -- forced a
        # real repaint. repaint() forces it synchronously, right here,
        # regardless of which event loop this ends up being called from.
        self.image_label.repaint()
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
        #
        # Backpressure: stop once the decode pool is already as busy as it
        # can usefully be. Confirmed from real logs that WebP decode
        # through this pipeline runs 100-300ms per image on this machine
        # -- with only _DECODE_WORKER_COUNT workers, rapid navigation
        # (e.g. holding an arrow key) can queue up preload jobs for
        # images already skipped past faster than the pool can clear
        # them, which pushed the currently-displayed image's own request
        # further back behind them in the same queue and made navigation
        # itself feel like it was lagging behind the keypresses. The
        # staleness check in _submit_image_load's worker already
        # discards a request once it *starts*, but that's after it
        # already occupied a worker slot for however long the job ahead
        # of it took; this stops it from being queued in the first place
        # when the pool has no spare capacity to begin with.
        generation = self.load_generation
        for distance in range(1, count + 1):
            for direction in (1, -1):
                if len(self.loading_keys) >= _DECODE_WORKER_COUNT:
                    return
                idx = self.current_index + (distance * direction)
                if 0 <= idx < len(self.image_list):
                    self._submit_image_load(idx, generation)

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

    def _build_qmovie(self, current_file, data, validate_first_frame=True):
        """Build one playable QMovie from either raw bytes (data, for a zip
        entry) or a path on disk (current_file, when data is None). Used
        both for the live/display movie and for the independent look-ahead
        movie in show_current_image, so the two always decode identically.

        validate_first_frame decodes frame 0 right away to confirm the
        movie actually plays (not just that QMovie recognized the
        format) -- needed the first time this data is built into a
        movie, but wasted work the second time: show_current_image
        builds a second, independent QMovie from the exact same bytes
        for read-ahead (self.prefetch_movie), and decoding frame 0
        twice back to back on the GUI thread was adding a full extra
        synchronous frame decode on top of the one already needed just
        to switch to a large animated file -- measured around 100ms+
        each for just a 1080p frame, so a real, directly-felt part of
        "opening a big webp is slow". Pass False for that second build:
        if the primary movie's identical data just decoded frame 0
        fine, this one will too."""
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
            if validate_first_frame:
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
            self.current_movie_source = self._animation_source_key(current_file)
            self.current_movie_generation += 1
            movie_generation = self.current_movie_generation
            self.current_movie_original_size = movie.currentPixmap().size()
            self.scroll_area.setWidgetResizable(self.fit_to_window)

            # Built before the scaled-size is applied below so
            # _apply_anim_scaled_size can sync both movies in one place.
            # validate_first_frame=False: the primary `movie` above just
            # proved this exact data decodes fine, so re-decoding frame 0
            # a second time here would only cost GUI-thread time without
            # learning anything new -- see _build_qmovie's docstring.
            self.prefetch_movie, self.prefetch_buffer = self._build_qmovie(current_file, movie_source, validate_first_frame=False)

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
            w = h = 0
            restored = None
            if frame_count and frame_count > 0:
                # Frames are now cached at their native/decode resolution
                # (see _animated_cache_key/_show_animated_pixmap), which
                # is scaled_size capped at native when zoomed in past 1:1
                # -- exactly what _anim_decode_size already computes for
                # QMovie itself. Using the raw (possibly much larger,
                # zoomed) target size here instead would overestimate
                # bytes per frame and size the cache smaller than the
                # memory budget actually allows.
                decode_size = self._anim_decode_size(scaled_size) if scaled_size else self.current_movie_original_size
                w = decode_size.width() if decode_size else self.current_movie_original_size.width()
                h = decode_size.height() if decode_size else self.current_movie_original_size.height()
                if w > 0 and h > 0:
                    # Budget = what's actually free on this machine (see
                    # animated_cache_budget_bytes), not a fixed ceiling.
                    # Cached replay needs the *whole* loop in this cache,
                    # and w x h here depends on when this runs: opening the
                    # file at launch happens before the window has been
                    # laid out, so the scroll area is still at its tiny
                    # default size, w x h comes out tiny, and any loop
                    # "fits". Navigating to the same file (or double-
                    # clicking it while the app is already running) uses
                    # the real on-screen size instead, and a long high-res
                    # loop then blew through the old fixed 4GB -- the cache
                    # never held the whole loop, so replay never engaged.
                    # (Earlier history: a 385-frame animation at
                    # ~17.3MB/frame only got 124 frames at the original
                    # 2GB budget.)
                    need_bytes = frame_count * w * h * 4
                    # A cache kept from the last time this animation was on
                    # screen (see _stash_animated_cache) is taken back
                    # first, before anything below can drop it to make room.
                    restored = self._take_retained_anim_cache(self.current_movie_source, frame_count, w, h)
                    # Free RAM is read once, before any retained cache is
                    # dropped; what dropping gives back is added on top.
                    avail = get_available_physical_memory()
                    freed = 0
                    if restored is None:
                        freed = self._reclaim_retained_anim_caches(need_bytes, avail)
                    budget = animated_cache_budget_bytes(avail, freed)
                    self.animated_frame_cache_limit = animated_cache_frame_limit(frame_count, w, h, budget)
                    if restored is not None:
                        # Already resident, whatever the free RAM says now.
                        self.animated_frame_cache_limit = max(self.animated_frame_cache_limit, frame_count)
                else:
                    self.animated_frame_cache_limit = max(24, min(frame_count, 300))
            else:
                self.animated_frame_cache_limit = 24

            self.prefetch_frame_count = frame_count if frame_count and frame_count > 0 else None

            # Per-frame durations for cached replay (see
            # _try_start_anim_cache_playback). Only for a webp that loops
            # forever and whose ANMF chunk count matches Pillow's frame
            # count -- anything else (gif, a finite loop count, an odd
            # file) just keeps QMovie driving every loop as before.
            self.anim_frame_delays = None
            if restored is not None:
                # Frames (and the durations they were timed by) kept from
                # last time; the file is unchanged, see
                # _animation_source_key.
                self.animated_frame_cache = restored['frames']
                self.anim_frame_delays = restored['delays']
            elif self.prefetch_frame_count and ext == '.webp':
                info = read_webp_animation_info(movie_source if movie_source is not None else current_file)
                if info:
                    durations, loop_count = info
                    # Only an endlessly looping webp whose ANMF chunk count
                    # matches the decoder's frame count can be replayed from
                    # the cache. 10ms floor: a 0ms duration would otherwise
                    # make the replay spin as fast as the GUI thread allows.
                    if loop_count == 0 and len(durations) == self.prefetch_frame_count:
                        self.anim_frame_delays = [max(10, d) for d in durations]

            # A new movie must reconnect slideshow loop counting.
            if self.slideshow_playing and self.slideshow_mode == 'loop':
                self.connect_gif_loop()
            movie.frameChanged.connect(self.on_animated_frame_changed)
            if restored is not None and self._start_retained_replay():
                # The whole loop is already cached: play it from the first
                # frame right now. QMovie stays unstarted unless replay has
                # to hand back (see _leave_anim_cache_mode).
                self._preload_neighbor_animations()
                return
            # 고해상도 애니메이션 webp: QMovie 가 GUI 스레드에서 프레임을 하나씩 푸는 대신
            # 작업 스레드 여러 개가 루프 전체를 미리 푼다 (아래 _start_first_loop_decode 참고).
            # QMovie 는 시작하지 않은 채로 두고, 이 방식이 중간에 실패하면 그때 시작한다.
            if (restored is None and self._first_loop_eligible(ext, frame_count, w, h)
                    and self._start_first_loop_decode(
                        movie_source if movie_source is not None else current_file,
                        frame_count, w, h, movie_generation)):
                return
            movie.start()
            start_frame = movie.currentFrameNumber()
            self._render_animated_frame(start_frame, movie_generation)
            self._prefetch_ahead(start_frame)
            self._preload_neighbor_animations()
            return

        max_size = self._target_decode_size()
        key = self._cache_key(current_file, saturation, brightness, contrast, max_size)
        cached = self.cache_manager.get(key)
        if cached is not None:
            self._display_pixmap(cached)
            self._preload_neighbors()
            self._preload_neighbor_animations()
            return

        # Keep the previous frame visible while the new image is decoding.
        # Clearing the label here caused the frequent black-screen effect during
        # rapid navigation. A successful decode will replace it atomically.
        self._submit_image_load(self.current_index, generation)
        self._preload_neighbors()
        self._preload_neighbor_animations()

    def _animated_cache_key(self, frame_number):
        # Deliberately does NOT include zoom/window size (it used to:
        # fit_to_window, zoom_factor, scroll_area size). Frames are now
        # color-adjusted and cached at their own native resolution (see
        # _show_animated_pixmap), with the zoom/window-fit scale applied
        # as a separate, cheap step every time a frame is shown -- so the
        # same cached, color-adjusted frame is valid at any zoom level or
        # window size, not just the one it happened to be computed at.
        # Previously, zooming or resizing the window mid-playback threw
        # away every cached frame and forced a full recompute of the
        # entire animation from scratch.
        return (frame_number,
                self.settings.get('anim_saturation', 100),
                self.settings.get('anim_brightness', 100),
                self.settings.get('anim_contrast', 100))

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
        # First loop done and every frame cached: stop decoding through
        # QMovie and replay from the cache instead.
        if self._try_start_anim_cache_playback(frame_number):
            return
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
            self._show_animated_pixmap(cached)
            return

        if self.anim_cache_playing:
            # QMovie is paused while cached replay drives the display, so
            # qimage above is the frame it stopped on, not frame_number's --
            # processing it here would cache the wrong picture under this
            # key. Hand playback back to QMovie instead.
            self._leave_anim_cache_mode()
            return

        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)
        if saturation == 100 and brightness == 100 and contrast == 100:
            # Native size -- no color math needed, so nothing to gain by
            # rendering straight to the display target the way this used
            # to. Caching (and scaling for display) the same way as the
            # adjusted tiers below means this frame stays valid across
            # zoom/window-resize changes too.
            pixmap = QPixmap.fromImage(qimage)
            self._store_animated_frame(key, pixmap)
            self._show_animated_pixmap(pixmap)
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
            self._show_animated_pixmap(pixmap)
            return

        # A look-ahead prefetch may already be processing this exact frame;
        # if so just wait for that result instead of computing it twice.
        if key in self.animated_inflight_keys:
            return
        self._submit_animated_frame_processing(qimage, frame_number, generation, key)

    def _show_animated_pixmap(self, native_pixmap):
        """Scale a native-resolution animated frame pixmap (cached or
        just computed -- animated_frame_cache always stores frames at
        their own native resolution now, see _animated_cache_key) to the
        current display target and show it. This is the one place that
        scale actually happens, separate from the color math, so a
        zoom or window-resize change only needs this cheap step, not a
        full recompute of every cached frame."""
        target = self.current_movie_target_size
        pixmap = native_pixmap
        if target and (pixmap.width() != target.width() or pixmap.height() != target.height()):
            mode = Qt.FastTransformation if self.settings.get('zoom_quality', 'balanced') == 'speed' else Qt.SmoothTransformation
            pixmap = pixmap.scaled(target, Qt.KeepAspectRatio, mode)
        # target is already a physical-pixel quantity (see the old
        # comment on _store_animated_frame) -- tag the pixmap actually
        # being shown, not the native-resolution one that may still be
        # sitting in animated_frame_cache.
        pixmap.setDevicePixelRatio(self.devicePixelRatioF())
        self.current_pixmap = pixmap
        self.image_label.setPixmap(pixmap)
        self.image_label.adjustSize()

    def _render_animated_frame_gpu(self, qimage, saturation, brightness, contrast):
        """GPU-shader replacement for the apply_color_adjustments()/
        apply_color_adjustments_cv2() call in _submit_animated_frame_
        processing, for the frame that's actually about to be displayed.
        Renders synchronously (see GpuColorCorrector.adjust) at the
        frame's own native resolution and returns that as a QPixmap, or
        None if the GPU path isn't available -- callers fall back to the
        unchanged cv2/Pillow tiers in that case. Scaling to the current
        display target happens separately in _show_animated_pixmap, same
        as the cv2/Pillow tiers already do (see the matching comment in
        _submit_animated_frame_processing) -- this used to render
        straight to current_movie_target_size instead, which meant a
        cached result was only ever valid at the exact zoom/window size
        it was computed for.

        No pixel-count cutoff before attempting this (an earlier version
        had one, out of unverified concern that a large frame's upload+
        render+readback could itself stall the GUI thread for a visible
        moment). Real logs from actual playback showed the opposite:
        ~1080x1080 GPU calls ran ~5ms, and gating out a zoomed 2068x2068
        frame (~4.3MP, over that old cutoff) forced it onto the cv2 tier
        instead at 25-45ms -- worse, not safer. If a genuinely large
        enough frame ever does make one GPU call slow enough to matter,
        that's a real data point to reintroduce a cutoff from; guessing
        a threshold with no measurement behind it did more harm than
        good here."""
        w, h = qimage.width(), qimage.height()
        result = self.gl_color_corrector.adjust(
            qimage, saturation / 100.0, brightness / 100.0, contrast / 100.0,
            w, h)
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
                #
                # No target_w/target_h passed here anymore -- output
                # stays at native (w, h) resolution so the cached,
                # color-adjusted result is reusable at any zoom/window
                # size, not just the one in effect right now.
                # _show_animated_pixmap does the (cheap) scale to
                # whatever the current display target is, separately,
                # every time a frame is actually shown, cached or not.
                result = _process_animated_frame_fast(raw, w, h, saturation, brightness, contrast, None, None)
                if result is not None:
                    return result
                try:
                    from PIL import Image
                    src = Image.frombuffer('RGBA', (w, h), raw, 'raw', 'RGBA', 0, 1)
                    # Keep the source alpha through the color math instead
                    # of letting convert('RGB') discard it -- see the
                    # matching fix in _process_animated_frame_fast/the GPU
                    # shader for why forcing full opacity here was turning
                    # transparent areas solid-colored.
                    alpha = src.getchannel('A')
                    rgb = src.convert('RGB')
                    rgb = apply_color_adjustments(rgb, saturation, brightness, contrast)
                    out = rgb.convert('RGBA')
                    out.putalpha(alpha)
                    return out.tobytes('raw', 'RGBA'), rgb.width, rgb.height
                except Exception as e:
                    print(f"[애니메이션 프레임 색보정 실패] {e}")
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
        """Decode ONE not-yet-cached frame from within the next
        anim_lookahead frames on the paused prefetch movie, and dispatch
        it to the cv2/Pillow worker pool now (see
        _submit_animated_frame_processing), so it's ready in
        animated_frame_cache before playback actually reaches it.

        Deliberately never uses the synchronous GPU tier
        (_render_animated_frame_gpu) here, even though
        _render_animated_frame does for the frame that's actually about
        to be displayed: prefetching exists precisely because these
        frames *aren't* needed yet, so there's no reason to pay a
        synchronous GUI-thread cost for them.

        Deliberately decodes at most ONE frame per call, not all of
        anim_lookahead at once, even though this function runs on every
        single frame change and so still fills the lookahead window over
        the next few frame changes either way. jumpToFrame()+
        currentImage() on prefetch_movie is a real, synchronous decode on
        the GUI thread -- the color math after it is what's offloaded to
        the worker pool, not this part -- so decoding up to
        anim_lookahead frames back-to-back in one call meant that on an
        uncached (first) loop, nearly every frame change paid for
        anim_lookahead-many synchronous decodes stacked on top of the
        one already needed for the live frame in _render_animated_frame.
        That's what was making a color-adjusted animation's first loop
        take much longer in wall-clock time than the file's own declared
        duration (e.g. a 6s loop measured closer to 11s+), independent of
        how fast the GPU/cv2 color pass itself is -- the decode this
        function does was the part actually stacking up."""
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
            # One synchronous decode is enough for this call -- see
            # docstring. The remaining lookahead frames get their turn on
            # the next few frame-changed events instead of all landing
            # here at once.
            return

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
            self._show_animated_pixmap(pixmap)

    def _store_animated_frame(self, key, pixmap):
        # Frames are cached at their own native resolution now (see
        # _animated_cache_key/_show_animated_pixmap) -- no devicePixelRatio
        # tagging here, since this pixmap isn't necessarily sized to any
        # particular display target yet. _show_animated_pixmap tags the
        # pixmap it actually puts on screen instead, after scaling it to
        # the current target.
        self.animated_frame_cache[key] = pixmap
        self.animated_frame_cache.move_to_end(key)
        while len(self.animated_frame_cache) > self.animated_frame_cache_limit:
            self.animated_frame_cache.popitem(last=False)

    def _animation_source_key(self, filepath):
        """Identity of an animation for the retained caches: which file (and
        zip) it is plus that file's size and modified time, so a file that
        has changed on disk is never replayed from its old frames. None if
        it can't be worked out."""
        try:
            st = os.stat(self.current_zip or filepath)
            return (self.current_zip, filepath, st.st_size, st.st_mtime_ns)
        except Exception:
            return None

    def _stash_animated_cache(self):
        """Called first thing by stop_current_movie, while the animation
        being left is still fully described by self.*. If its whole loop is
        sitting in animated_frame_cache, preloading is on and the machine can
        spare the RAM, move it into retained_anim_caches instead of letting
        stop_current_movie throw it away -- so coming back to the animation
        replays from the cache at once instead of decoding the first loop
        all over again (an image you leave stays cached the same way).
        Returns True if the frames were kept, in which case the caller must
        not clear() them."""
        try:
            if not (self.preload_enabled and self.preload_count > 0):
                # Retention is off (e.g. preloading was just switched off in
                # the settings): also let go of whatever was kept while it
                # was on, rather than sit on that memory.
                self.retained_anim_caches.clear()
                return False
            key = self.current_movie_source
            total = self.prefetch_frame_count
            delays = self.anim_frame_delays
            cache = self.animated_frame_cache
            if (not self.current_movie or key is None or not delays or not total
                    or len(delays) != total or len(cache) < total):
                return False
            keys = [self._animated_cache_key(i) for i in range(total)]
            if not all(k in cache for k in keys):
                return False
            last = cache[keys[-1]]
            entry = {
                'frames': cache,
                'delays': list(delays),
                'total': total,
                'bytes': sum(pm.width() * pm.height() * 4 for pm in cache.values()),
                'frame_size': (last.width(), last.height()),
            }
        except Exception:
            return False
        try:
            self.retained_anim_caches.pop(key, None)
            self.retained_anim_caches[key] = entry
            # current_index is already the image being navigated TO here
            # (see next_image/prev_image); if a cache for it is waiting in
            # the pool, it is about to be taken -- don't let it cost a
            # neighbor its slot meanwhile (see _trim_retained_anim_caches).
            arrival_key = None
            if 0 <= self.current_index < len(self.image_list):
                arrival_key = self._animation_source_key(self.image_list[self.current_index])
            self._trim_retained_anim_caches(spare_key=arrival_key if arrival_key in self.retained_anim_caches else None)
            kept = key in self.retained_anim_caches
            return kept
        except Exception:
            # Never let bookkeeping get in the way of navigating: fall back
            # to the old behaviour (the caller clears the frames).
            self.retained_anim_caches.pop(key, None)
            return False

    def _retained_anim_distance(self, key):
        """How far, in list positions, a retained cache's file is from the
        image on screen now; infinity if it isn't in the current list at
        all (left over from another folder or zip)."""
        try:
            return abs(self.image_list.index(key[1]) - self.current_index)
        except Exception:
            return float('inf')

    def _retained_eviction_order(self):
        """Retained caches in the order they should be dropped: farthest
        from the image on screen first -- the least likely to be visited
        next. This used to be oldest-first, which threw away exactly the
        neighbor that had just been prepared for the image being navigated
        to: leaving an animation stashes ITS cache, and with the pool at
        its limit the oldest entry -- the prepared next image -- went to
        make room, so preloading never paid off however long you waited.
        sorted() is stable, so equally far (or all unknown) entries keep
        their insertion order, i.e. the least recently kept goes first."""
        return sorted(self.retained_anim_caches, key=lambda k: -self._retained_anim_distance(k))

    def _trim_retained_anim_caches(self, spare_key=None):
        """Keep the retained caches within ANIMATED_CACHE_RETAIN_MAX entries
        and within what the machine can spare -- the RAM free right now plus
        what the retained caches themselves hold, at the same 80% rule as
        animated_cache_budget_bytes -- dropping the farthest from the image
        on screen first (see _retained_eviction_order).

        spare_key is an entry that doesn't count toward the entry limit and
        is never dropped for it: while an animation is being left, the
        cache of the one about to be shown is still sitting in the pool
        (it is taken out again a moment later, and becomes the on-screen
        animation's own cache), so counting it would push out a neighbor
        that's wanted only to have it prepared all over again."""
        pool = self.retained_anim_caches
        while len([k for k in pool if k != spare_key]) > ANIMATED_CACHE_RETAIN_MAX:
            pool.pop([k for k in self._retained_eviction_order() if k != spare_key][0])
        total = sum(e['bytes'] for e in pool.values())
        budget = animated_cache_budget_bytes(extra_bytes=total)
        while pool and total > budget:
            dropped = pool.pop(self._retained_eviction_order()[0])
            total -= dropped['bytes']

    def _take_retained_anim_cache(self, key, frame_count, w, h):
        """Take this animation's retained cache out of the pool if it's still
        good for what's about to be shown: same frame count, every frame
        present under the current anim color settings, and frames the size a
        fresh decode would use now (w x h -- i.e. window/zoom unchanged).
        Otherwise a fresh decode beats replaying frames of the wrong size or
        color, and the entry is dropped rather than left sitting on memory.
        Returns the entry, or None."""
        entry = self.retained_anim_caches.pop(key, None) if key is not None else None
        if entry is None:
            return None
        reason = None
        try:
            frames = entry['frames']
            fw, fh = entry['frame_size']
            if entry['total'] != frame_count or len(frames) < frame_count:
                reason = '프레임 수가 달라서'
            elif not all(self._animated_cache_key(i) in frames for i in range(frame_count)):
                reason = '애니메이션 색 보정 설정이 바뀌어서'
            elif abs(fw - w) > 2 or abs(fh - h) > 2:
                reason = f'화면 크기가 바뀌어서(보관 {fw}x{fh} / 지금 {w}x{h})'
        except Exception:
            reason = '보관된 캐시를 확인하지 못해서'
        if reason:
            return None
        return entry

    def _reclaim_retained_anim_caches(self, need_bytes, avail):
        """Retained caches are a convenience, so they give way to the
        animation being loaded: if its whole loop wouldn't fit in the RAM
        that's free (avail, read once by the caller before anything is
        dropped), drop the least recently kept retained caches until it
        does, or none are left. Returns how many bytes that gave back -- to
        be added to the budget, since the memory only becomes free after
        avail was read."""
        pool = self.retained_anim_caches
        freed = 0
        while pool and need_bytes > animated_cache_budget_bytes(avail, freed):
            dropped = pool.pop(self._retained_eviction_order()[0])
            freed += dropped['bytes']
        return freed

    def _start_retained_replay(self):
        """Start cached replay at once from a cache restored by
        _take_retained_anim_cache, without ever starting QMovie (it is only
        started if replay has to hand back, see _leave_anim_cache_mode).
        Frame 0 goes on screen now and the rest follow on their own
        durations. Returns False if it can't -- the caller then just starts
        QMovie as usual, and the still-complete cache makes
        _try_start_anim_cache_playback take over on frame 0 anyway."""
        total = self.prefetch_frame_count
        delays = self.anim_frame_delays
        if not self.current_movie or not total or not delays or len(delays) != total:
            return False
        key = self._animated_cache_key(0)
        pixmap = self.animated_frame_cache.get(key)
        if pixmap is None:
            return False
        self.animated_frame_cache.move_to_end(key)
        self.anim_cache_playing = True
        self.anim_cache_index = 0
        self.current_movie_frame = 0
        self.on_gif_frame_changed(0)
        self._show_animated_pixmap(pixmap)
        self._anim_cache_deadline = time.perf_counter()
        self._schedule_next_cached_frame()
        return True

    def _current_preload_wanted_keys(self, include_current=False):
        """_animation_source_key() for every file presently within the
        preload range (plus the image on screen itself when
        include_current). Recomputed fresh, never cached, since it's read
        both right before a submission and, later, when that job's result
        is being admitted -- by which time real navigation may have moved
        on. Used so preparing one neighbor never evicts a retained entry
        that's ALSO still wanted: that one would just be resubmitted."""
        if not self.image_list:
            return set()
        count = max(0, min(10, int(self.preload_count)))
        keys = set()
        for distance in range(1, count + 1):
            for direction in (1, -1):
                idx = self.current_index + (distance * direction)
                if 0 <= idx < len(self.image_list):
                    k = self._animation_source_key(self.image_list[idx])
                    if k is not None:
                        keys.add(k)
        if include_current and 0 <= self.current_index < len(self.image_list):
            k = self._animation_source_key(self.image_list[self.current_index])
            if k is not None:
                keys.add(k)
        return keys

    def _anim_evictable_keys(self, protected=None):
        """Retained caches that can be dropped to make room for a newly
        prepared neighbor without costing anything still wanted -- neither
        within the preload range nor the image on screen -- farthest from
        the image on screen first."""
        if protected is None:
            protected = self._current_preload_wanted_keys(include_current=True)
        return [k for k in self._retained_eviction_order() if k not in protected]

    def _preload_neighbor_animations(self):
        # Runs at the very end of show_current_image and from timer/signal
        # slots: a bug in this background preparation must never turn into
        # an exception that breaks navigation or the event loop.
        try:
            self._preload_neighbor_animations_impl()
        except Exception as e:
            print(f"[애니메이션 미리 디코딩 오류] {e}")

    def _preload_neighbor_animations_impl(self):
        """Background-decode whole loops of eligible neighbor animations
        (same eligibility as cached replay -- see where show_current_image
        fills anim_frame_delays: an infinite-loop webp whose container frame
        count matches Pillow's) within the preload range, straight into
        retained_anim_caches -- the same place a *revisited* animation's
        cache is kept (_stash_animated_cache). So by the time navigation
        actually reaches one, _take_retained_anim_cache finds it already
        warm and _start_retained_replay begins cached playback on frame 0
        at once, instead of a first loop through QMovie.

        Gated on preload_enabled/preload_count exactly like
        _preload_neighbors (this piggybacks on that setting rather than
        adding a separate one).
        Uses its own tiny executor (ImageLoader._anim_preload_executor,
        sized _ANIM_PRELOAD_WORKER_COUNT=1) -- never the static-image pool
        or the live-playback per-frame pool. One neighbor is prepared at a
        time, nearest first; the slot stays taken until its pixmaps have
        been built too (_begin_anim_admission), and farther ones wait
        their turn as it frees up (called again when one finishes) or
        navigation continues (called again from show_current_image)."""
        if not (self.preload_enabled and self.image_list):
            return
        count = max(0, min(10, int(self.preload_count)))
        if count <= 0:
            return

        # A file declined earlier for lack of room (see
        # _finish_anim_admission) deserves a fresh look once the pool has
        # actually lost a member since -- that is the only thing that
        # could free room for it. This is checked here (every call this
        # function makes it past the gates above), not cleared on plain
        # navigation: back-and-forth browsing that never actually frees a
        # slot must not keep re-decoding the same too-big-to-fit file from
        # scratch on every step -- which is exactly what unconditionally
        # clearing this on every navigation used to do.
        current_pool_size = len(self.retained_anim_caches)
        if current_pool_size < self._anim_declined_pool_size:
            self.anim_preload_declined.clear()
        self._anim_declined_pool_size = current_pool_size

        wanted = []
        for distance in range(1, count + 1):
            for direction in (1, -1):
                idx = self.current_index + (distance * direction)
                if 0 <= idx < len(self.image_list):
                    wanted.append(self.image_list[idx])

        # A neighbor that fell out of range since it was submitted (the
        # user kept navigating) no longer needs preparing -- let its
        # worker, or the pixmap conversion that follows it, notice and
        # stop early.
        wanted_set = set(wanted)
        for fname, cancel_event in list(self.anim_preload_inflight.items()):
            if fname not in wanted_set:
                cancel_event.set()

        if len(self.anim_preload_inflight) >= _ANIM_PRELOAD_WORKER_COUNT:
            return

        if (self.current_movie and self.anim_frame_delays
                and (not self.anim_cache_playing or self._first_loop_job is not None)):
            # The animation on screen is still filling its own frame cache
            # (its slow first loop). That's when the GUI thread is busiest
            # and when its memory use is still growing, so preparing
            # neighbors now would compete with exactly what the person is
            # waiting on. Wait until it switches to cached replay (which
            # calls back into here, see _try_start_anim_cache_playback).
            return

        dpr = self.devicePixelRatioF()
        fit_to_window = self.fit_to_window
        # Exactly the QSize the live path builds (QSize * dpr rounds the
        # way Qt does), so preloaded frames come out the same size.
        box = self.scroll_area.size() * dpr
        box_w = box.width() if fit_to_window else None
        box_h = box.height() if fit_to_window else None
        zoom_factor = self.zoom_factor
        reserve_bytes = self._current_anim_pending_cache_bytes()
        saturation = self.settings.get('anim_saturation', 100)
        brightness = self.settings.get('anim_brightness', 100)
        contrast = self.settings.get('anim_contrast', 100)

        # Every candidate's source key, computed once up front: needed
        # both to skip an already-retained/ineligible one below, and (as
        # protected) to tell a genuinely-stale retained entry from one
        # that's still in range -- see the room check below.
        candidate_keys = {}
        for filename in wanted:
            if os.path.splitext(filename)[1].lower() == '.webp':
                k = self._animation_source_key(filename)
                if k is not None:
                    candidate_keys[filename] = k
        protected = set(candidate_keys.values())
        if 0 <= self.current_index < len(self.image_list):
            current_key = self._animation_source_key(self.image_list[self.current_index])
            if current_key is not None:
                protected.add(current_key)

        for filename in wanted:
            if os.path.splitext(filename)[1].lower() != '.webp':
                # Only ever eligible kind for cached replay -- see
                # show_current_image. Preloading a gif or a finite-loop
                # webp's frames would still skip their first-loop color-
                # adjustment cost, but the current retained-cache format
                # requires anim_frame_delays (webp, loop=0), and extending
                # that is a larger change than this preload feature.
                continue
            if filename in self.anim_preload_inflight:
                continue
            key = candidate_keys.get(filename)
            if (key is None or key in self.retained_anim_caches
                    or key in self.anim_preload_ineligible or key in self.anim_preload_declined):
                continue
            if len(self.retained_anim_caches) + len(self.anim_preload_inflight) >= ANIMATED_CACHE_RETAIN_MAX:
                # No free slot. Worth trying only if at least one
                # currently-retained entry is a genuine eviction target
                # (not in range, not on screen) -- otherwise preparing this
                # one would just force out another that's equally still
                # wanted, which would then immediately be resubmitted right
                # back. Nothing else in `wanted` can do better than this
                # same check, so stop for this call.
                if not self._anim_evictable_keys(protected):
                    return
            self._submit_anim_preload(filename, key, fit_to_window, box_w, box_h,
                                       zoom_factor, saturation, brightness, contrast,
                                       reserve_bytes)
            return

    def _current_anim_pending_cache_bytes(self):
        """Bytes the animation on screen has still to add to its own frame
        cache (0 if none is playing or it's already full). Reserved out of
        the free RAM a neighbor preload may claim, since that memory is
        about to be taken."""
        try:
            if not self.current_movie:
                return 0
            missing = self.animated_frame_cache_limit - len(self.animated_frame_cache)
            if missing <= 0:
                return 0
            if self.animated_frame_cache:
                pm = next(reversed(self.animated_frame_cache.values()))
                per_frame = pm.width() * pm.height() * 4
            else:
                size = self.current_movie_target_size or self.current_movie_original_size
                size = self._anim_decode_size(size) if size else None
                per_frame = size.width() * size.height() * 4 if size else 0
            return missing * per_frame
        except Exception:
            return 0

    def _submit_anim_preload(self, filename, key, fit_to_window, box_w, box_h,
                              zoom_factor, saturation, brightness, contrast,
                              reserve_bytes=0):
        """Kick off the background decode for one neighbor (see
        _preload_neighbor_animations). Everything the worker needs is
        captured here, on the GUI thread, as plain values -- never a Qt
        object, never a live read of self.settings/self.current_zip from
        the worker thread itself (same discipline _submit_image_load and
        _submit_animated_frame_processing already follow).

        reserve_bytes is memory the animation on screen is about to take
        for its own cache: the worker refuses to start a decode whose
        result the *remaining* free RAM couldn't comfortably hold."""
        source_zip = self.current_zip
        cancel_event = threading.Event()
        self.anim_preload_inflight[filename] = cancel_event

        def worker():
            _lower_current_thread_to_background_priority()
            try:
                data = None
                if source_zip:
                    try:
                        zf = ZipHandler._get_zip(source_zip)
                        with zf.open(filename, 'r') as fp:
                            data = fp.read()
                    except Exception:
                        return None
                if cancel_event.is_set():
                    return None

                # False below means "this file itself will never qualify,
                # regardless of window/zoom/memory state" -- see
                # anim_preload_ineligible above. None means "worth trying
                # again later" (cancelled, or no room right now).
                frame_count = get_frame_count(BytesIO(data) if data is not None else filename)
                if not frame_count or frame_count <= 1:
                    return False
                info = read_webp_animation_info(data if data is not None else filename)
                if not info:
                    return False
                durations, loop_count = info
                if loop_count != 0 or len(durations) != frame_count:
                    return False
                delays = [max(10, d) for d in durations]

                Image = get_pil_image()
                with Image.open(BytesIO(data) if data is not None else filename) as im:
                    orig_w, orig_h = im.size
                target_w, target_h = compute_anim_decode_size(
                    orig_w, orig_h, fit_to_window, box_w, box_h, zoom_factor)
                if target_w <= 0 or target_h <= 0:
                    # Only reachable via a corrupt/zero-sized orig_w/orig_h
                    # from Pillow -- compute_anim_decode_size itself never
                    # returns non-positive from a positive input.
                    return False

                need_bytes = frame_count * target_w * target_h * 4
                avail = get_available_physical_memory()
                if avail is not None:
                    avail = max(0, avail - reserve_bytes)
                budget = animated_preload_budget_bytes(avail)
                if need_bytes > budget:
                    return None
                if cancel_event.is_set():
                    return None

                frames = decode_webp_animation_frames(
                    BytesIO(data) if data is not None else filename,
                    frame_count, target_w, target_h, saturation, brightness, contrast,
                    cancel_event.is_set)
                if not frames:
                    # Cancelled (the neighbor fell out of range mid-decode)
                    # is transient; an actual decode failure on this data
                    # (past the lightweight container checks above) is not.
                    return None if cancel_event.is_set() else False
                total_bytes = sum(len(raw) for raw, _, _ in frames)
                return {'frames': frames, 'delays': delays, 'total': frame_count,
                        'frame_size': (target_w, target_h), 'bytes': total_bytes,
                        'saturation': saturation, 'brightness': brightness, 'contrast': contrast}
            except Exception as e:
                print(f"[애니메이션 미리 디코딩 오류] {os.path.basename(filename)}: {e}")
                return None

        future = ImageLoader._anim_preload_executor.submit(worker)
        def done(fut, key=key, filename=filename):
            try:
                result = fut.result()
            except Exception:
                result = None
            self.load_bridge.anim_preload_ready.emit(key, (filename, result))
        future.add_done_callback(done)

    def _mark_anim_ineligible(self, key):
        if key is None:
            return
        self.anim_preload_ineligible[key] = True
        self.anim_preload_ineligible.move_to_end(key)
        while len(self.anim_preload_ineligible) > 500:
            self.anim_preload_ineligible.popitem(last=False)

    def _mark_anim_declined(self, key):
        if key is None:
            return
        self.anim_preload_declined[key] = True
        self.anim_preload_declined.move_to_end(key)
        while len(self.anim_preload_declined) > 100:
            self.anim_preload_declined.popitem(last=False)

    def _on_anim_preload_ready(self, key, payload):
        filename, result = payload
        if result is False:
            self.anim_preload_inflight.pop(filename, None)
            self._mark_anim_ineligible(key)
            # Safe to chain: this candidate is now excluded, so each
            # chained call strictly shrinks the pool of candidates left.
            self._preload_neighbor_animations()
            return
        if not result:
            # Transient (cancelled, or not enough memory right now):
            # nothing has changed since this job was submitted, so chaining
            # straight into another attempt at the same candidate could
            # spin indefinitely if it's the only one in range. Leave it for
            # a later navigation to retry.
            self.anim_preload_inflight.pop(filename, None)
            return
        # The decode itself is done, but its frames are still raw bytes.
        # Turning them into QPixmaps has to happen on this (GUI) thread;
        # all of them in one go used to freeze playback for seconds and
        # hold a second copy of the whole loop in memory meanwhile, so it
        # is done a frame at a time instead.
        self._begin_anim_admission(key, filename, result)

    def _anim_admission_problem(self, key, filename):
        """Why a finished decode should NOT be admitted into the retained
        pool right now, or None if it can be. Checked when its conversion
        starts and again when it finishes, since navigation may have moved
        on in between."""
        # The file's identity is re-checked against what it is *now*, not
        # what it was when the job started -- it may have changed on disk.
        if key is None or self._animation_source_key(filename) != key:
            return 'changed'
        if key in self.retained_anim_caches:
            return 'have'
        if not (self.preload_enabled and self.preload_count > 0 and self.image_list):
            return 'off'
        if 0 <= self.current_index < len(self.image_list) and self.image_list[self.current_index] == filename:
            # Navigation reached this very file while it was being
            # prepared: it is on screen now, being decoded the ordinary
            # way, and must not also be kept in the pool.
            return 'current'
        if len(self.retained_anim_caches) >= ANIMATED_CACHE_RETAIN_MAX and not self._anim_evictable_keys():
            # Every retained entry is equally still wanted: keeping this
            # one would just push another out, to be prepared again.
            return 'full'
        return None

    def _begin_anim_admission(self, key, filename, result):
        st = {'key': key, 'filename': filename, 'result': result, 'index': 0,
              'frames_dict': OrderedDict(),
              # Pacing (see _anim_admit_step_impl): how long one frame's
              # conversion has taken lately, and how many times in a row a
              # step has been put off to keep clear of a replay tick.
              'cost_ms': 8.0, 'deferrals': 0}
        problem = self._anim_admission_problem(key, filename)
        if problem:
            self._end_anim_admission(st, chain=False, declined=(problem == 'full'))
            return
        self.anim_admit = st
        QTimer.singleShot(0, self._anim_admit_step)

    def _anim_admit_step(self):
        st = self.anim_admit
        if not st:
            return
        try:
            self._anim_admit_step_impl(st)
        except Exception as e:
            print(f"[애니메이션 미리 디코딩 오류] {os.path.basename(st['filename'])}: {e}")
            self._end_anim_admission(st, chain=False)

    def _anim_admit_step_impl(self, st):
        """Convert ONE frame to a QPixmap, release its raw bytes, and
        schedule the next -- so playback of the animation on screen only
        ever waits for a single frame's conversion at a time, never the
        whole loop's."""
        filename = st['filename']
        cancel_event = self.anim_preload_inflight.get(filename)
        if (cancel_event is None or cancel_event.is_set()
                or not (self.preload_enabled and self.preload_count > 0)
                or (0 <= self.current_index < len(self.image_list)
                    and self.image_list[self.current_index] == filename)):
            # Fell out of range (or was reached, or preloading was turned
            # off) while converting: stop and give the memory back.
            self._end_anim_admission(st, chain=False)
            return
        if self.anim_cache_playing and st['deferrals'] < 4:
            # A cached replay is running: never let this conversion delay
            # its next tick. If that tick is due before a frame's worth of
            # conversion would finish, run right after it instead (that
            # tick then leaves a whole frame duration free). After a few
            # such put-offs in a row -- frames so short there is never
            # room -- go ahead anyway rather than starve.
            slack_ms = (self._anim_cache_deadline - time.perf_counter()) * 1000.0
            if slack_ms < st['cost_ms'] * 1.3 + 2.0:
                st['deferrals'] += 1
                QTimer.singleShot(max(1, int(slack_ms) + 1), self._anim_admit_step)
                return
        st['deferrals'] = 0
        result = st['result']
        frames = result['frames']
        i = st['index']
        t0 = time.perf_counter()
        raw, w, h = frames[i]
        frames[i] = None   # the raw copy goes as soon as the pixmap exists
        qimg = QImage(raw, w, h, w * 4, QImage.Format_RGBA8888).copy()
        pixmap = QPixmap.fromImage(qimg)
        del raw, qimg
        if pixmap.isNull():
            # Deterministic for this data -- retrying would just fail the
            # same way every time.
            self._end_anim_admission(st, chain=True, ineligible=True)
            return
        # Same key shape as _animated_cache_key, built from the settings
        # captured when this job was submitted -- not a live re-read --
        # since that's what these pixels actually reflect. If
        # anim_saturation/brightness/contrast changed since,
        # _take_retained_anim_cache's own check (against the *current*
        # settings, when the animation is actually opened) refuses the
        # entry then -- same protection a stash-on-leave entry gets.
        st['frames_dict'][(i, result['saturation'], result['brightness'], result['contrast'])] = pixmap
        st['index'] = i + 1
        cost_ms = (time.perf_counter() - t0) * 1000.0
        # Remember the pessimistic side: one slow frame shouldn't be
        # forgotten by the very next step.
        st['cost_ms'] = max(cost_ms, st['cost_ms'] * 0.8)
        if st['index'] < len(frames):
            # Leave the event loop for about as long as the conversion
            # just took (at least a few ms), so this uses at most about
            # half of the GUI thread however heavy the frames are.
            QTimer.singleShot(max(2, int(cost_ms)), self._anim_admit_step)
            return
        self._finish_anim_admission(st)

    def _finish_anim_admission(self, st):
        key, filename, result = st['key'], st['filename'], st['result']
        problem = self._anim_admission_problem(key, filename)
        if problem:
            self._end_anim_admission(st, chain=False, declined=(problem == 'full'))
            return
        if len(self.retained_anim_caches) >= ANIMATED_CACHE_RETAIN_MAX:
            # _anim_admission_problem just confirmed there is an entry that
            # is not wanted: drop the farthest such one.
            self.retained_anim_caches.pop(self._anim_evictable_keys()[0], None)
        entry = {'frames': st['frames_dict'], 'delays': result['delays'],
                 'total': result['total'], 'bytes': result['bytes'],
                 'frame_size': result['frame_size']}
        self.retained_anim_caches[key] = entry
        self._trim_retained_anim_caches()
        kept = key in self.retained_anim_caches
        # Only chain into the next candidate after a success: after a
        # result that was thrown away, the same conditions would just
        # discard the next one too, each after a full decode.
        self._end_anim_admission(st, chain=kept, declined=not kept)

    def _end_anim_admission(self, st, chain, declined=False, ineligible=False):
        self.anim_admit = None
        self.anim_preload_inflight.pop(st['filename'], None)
        # Drop whatever this conversion still holds: raw frames not yet
        # converted, and (unless they were handed to the pool) the pixmaps.
        st['result']['frames'] = None
        st['frames_dict'] = None
        if ineligible:
            self._mark_anim_ineligible(st['key'])
        if declined:
            self._mark_anim_declined(st['key'])
        if chain:
            self._preload_neighbor_animations()

    def _stop_anim_cache_playback(self):
        self.anim_cache_timer.stop()
        self.anim_cache_playing = False
        self.anim_cache_index = -1

    def _leave_anim_cache_mode(self):
        """Give playback back to QMovie -- cached replay can't continue
        (a frame is missing from the cache, or something went wrong). QMovie
        picks up from the frame it was paused on; a later loop can switch
        back to cached replay once the cache covers the loop again."""
        was_playing = self.anim_cache_playing
        # 첫 루프 병렬 디코딩 중이었다면 그것도 멈춘다 (QMovie 가 이어받으므로).
        self._cancel_first_loop_decode()
        self._stop_anim_cache_playback()
        if was_playing and self.current_movie:
            movie = self.current_movie
            if movie.state() == QMovie.NotRunning:
                # Replay began straight from a retained cache, so QMovie
                # was never started.
                movie.start()
            else:
                movie.setPaused(False)

    # ------------------------------------------------------------------
    # 고해상도 애니메이션 webp: 첫 루프 병렬 디코딩
    # (작업 자체는 모듈 위쪽의 FirstLoopJob 이 GUI 스레드 밖에서 한다)
    # ------------------------------------------------------------------
    def _first_loop_eligible(self, ext, frame_count, w, h):
        """이 애니메이션의 첫 루프를 QMovie 대신 병렬 디코딩으로 채울지. 캐시 재생이 가능한
        webp (anim_frame_delays 가 있고 루프 전체가 캐시에 들어감) 중에서도 프레임이 충분히
        커서 QMovie 로는 느린 것만 해당한다. 나머지는 예전 방식 그대로."""
        try:
            if ext != '.webp':
                return False
            delays = self.anim_frame_delays
            if (not delays or not frame_count or frame_count < ANIMATED_FIRST_LOOP_MIN_FRAMES
                    or len(delays) != frame_count):
                return False
            if self.animated_frame_cache_limit < frame_count or w <= 0 or h <= 0:
                return False
            orig = self.current_movie_original_size
            if not orig or orig.width() * orig.height() < ANIMATED_FIRST_LOOP_MIN_PIXELS:
                return False
            return True
        except Exception:
            return False

    def _start_first_loop_decode(self, source, frame_count, w, h, movie_generation):
        """작업 스레드 여러 개가 루프 전체를 w x h 로 풀도록 시작한다. QMovie 는 시작하지 않고,
        프레임이 나오는 대로 _first_loop_pump 가 캐시에 넣는다. 재생은 기존 캐시 재생
        (_anim_cache_tick) 이 하되, 디코딩이 재생을 따라잡을 수 있다고 판단되는 시점
        (first_loop_can_start) 부터 시작한다. 시작했으면 True."""
        try:
            if ImageLoader._shutdown:
                ImageLoader.restart_executor()
            job = FirstLoopJob(
                movie_generation, source, frame_count, w, h,
                self.settings.get('anim_saturation', 100),
                self.settings.get('anim_brightness', 100),
                self.settings.get('anim_contrast', 100),
                self.anim_frame_delays)
            self._first_loop_job = job
            job.start(ImageLoader._first_loop_executor)
            self._first_loop_timer.start(8)
            return True
        except Exception as e:
            print(f"[첫 루프 병렬 디코딩 오류] 시작하지 못했습니다: {e}")
            self._cancel_first_loop_decode()
            return False

    def _cancel_first_loop_decode(self):
        job = self._first_loop_job
        self._first_loop_job = None
        try:
            self._first_loop_timer.stop()
        except Exception:
            pass
        if job is not None:
            job.cancel()
            job.release()

    def _first_loop_pump(self):
        """8ms 마다: 작업 스레드가 내놓은 프레임을 캐시로 옮기고, 재생을 시작할 때가 됐는지 본다."""
        job = self._first_loop_job
        if job is None:
            self._first_loop_timer.stop()
            return
        try:
            if job.generation != self.current_movie_generation or not self.current_movie:
                self._cancel_first_loop_decode()
                return
            sig = (self.settings.get('anim_saturation', 100),
                   self.settings.get('anim_brightness', 100),
                   self.settings.get('anim_contrast', 100))
            if sig != job.settings_sig:
                raise RuntimeError('애니메이션 색 보정 설정이 바뀌었습니다')
            if job.error:
                raise RuntimeError(job.error)
            # 창을 키웠거나 확대해서 필요한 디코딩 크기가 작업 크기보다 훨씬 커졌으면 새 크기로
            # 다시 푼다. 파일을 실행하며 바로 열 때가 대표적이다: 창이 아직 배치되기 전 (작은
            # 크기) 에 이 작업이 시작되는데, 그대로 두면 모든 프레임이 작은 크기로 풀려 흐릿하다.
            target = self.current_movie_target_size
            if target and target.width() > 0 and target.height() > 0:
                need = self._anim_decode_size(target)
                if (need.width() > job.target_w * 1.15 or need.height() > job.target_h * 1.15) \
                        and time.perf_counter() - self._first_loop_last_restart >= 0.4:
                    self._first_loop_restart(job, need)
                    return
            self._first_loop_convert_ready(job)
            if self._first_loop_job is not job:
                return
            self._first_loop_maybe_play(job)
            if self._first_loop_job is job and job.converted >= job.frame_count:
                self._first_loop_done(job)
        except Exception as e:
            print(f"[첫 루프 병렬 디코딩 오류] {e} -- QMovie 재생으로 되돌립니다")
            self._first_loop_fallback()

    def _first_loop_convert_ready(self, job):
        """작업 스레드가 내놓은 프레임을 (시간 예산 안에서) QPixmap 으로 바꿔 캐시에 넣는다.
        QPixmap 은 GUI 스레드에서만 만들 수 있다."""
        stop_at = time.perf_counter() + 0.005
        while True:
            try:
                idx, raw, w, h, opaque = job.results.get_nowait()
            except queue.Empty:
                return
            # 알파가 없는 프레임은 RGBX 로 넘겨서 알파 곱셈 변환을 피하고 불투명 픽스맵으로 만든다.
            fmt = QImage.Format_RGBX8888 if opaque else QImage.Format_RGBA8888
            qimg = QImage(raw, w, h, w * 4, fmt)
            pixmap = QPixmap.fromImage(qimg)
            del qimg
            if pixmap.isNull():
                raise RuntimeError(f'프레임 {idx} 를 QPixmap 으로 바꾸지 못했습니다')
            self._store_animated_frame(self._animated_cache_key(idx), pixmap)
            job.mark_converted(idx)
            if idx == 0 and not job.first_shown and not self.anim_cache_playing:
                job.first_shown = True
                self.current_movie_frame = 0
                self._show_animated_pixmap(pixmap)
            if time.perf_counter() >= stop_at:
                return

    def _first_loop_begin_playback(self, job):
        """캐시 재생을 0번 프레임부터 시작한다 (_start_retained_replay 와 같은 방식)."""
        key0 = self._animated_cache_key(0)
        pixmap = self.animated_frame_cache.get(key0)
        if pixmap is None:
            return False
        job.playing = True
        self.animated_frame_cache.move_to_end(key0)
        self.anim_cache_playing = True
        self.anim_cache_index = 0
        self.current_movie_frame = 0
        self.on_gif_frame_changed(0)
        self._anim_cache_deadline = time.perf_counter()
        self._schedule_next_cached_frame()
        return True

    def _first_loop_maybe_play(self, job):
        if job.playing or self.anim_cache_playing:
            return
        if job.prefix < min(2, job.frame_count):
            return
        elapsed = time.perf_counter() - job.t0
        if not (first_loop_can_start(job.frame_count, job.decoded, elapsed, job.cum_s,
                                      _FIRST_LOOP_WORKER_COUNT)
                or elapsed >= ANIMATED_FIRST_LOOP_MAX_PREBUFFER):
            return
        self._first_loop_begin_playback(job)

    def _first_loop_done(self, job):
        """모든 프레임이 캐시에 들어갔다: 이후는 기존 캐시 재생이 그대로 이어받는다."""
        self._first_loop_timer.stop()
        self._first_loop_job = None
        job.release()
        if not job.playing and not self.anim_cache_playing:
            self._first_loop_begin_playback(job)
        # 첫 루프 준비가 끝나서 GUI 스레드가 한가해졌으니 이웃 미리 디코딩을 시작해도 된다.
        self._preload_neighbor_animations()

    def _first_loop_fallback(self):
        """병렬 디코딩을 포기하고 QMovie 재생으로 되돌린다. 이미 캐시에 들어간 프레임은 그대로
        쓰인다."""
        self._cancel_first_loop_decode()
        movie = self.current_movie
        if not movie:
            return
        try:
            if self.anim_cache_playing:
                # QMovie 를 시작하거나 재개한다.
                self._leave_anim_cache_mode()
            else:
                movie.start()
                start_frame = movie.currentFrameNumber()
                self._render_animated_frame(start_frame, self.current_movie_generation)
                self._prefetch_ahead(start_frame)
        except Exception as e:
            print(f"[첫 루프 병렬 디코딩 오류] QMovie 재생으로 되돌리지 못했습니다: {e}")
        self._preload_neighbor_animations()

    def _first_loop_restart(self, job, need):
        """필요한 디코딩 크기가 커졌을 때 (창 확대, 확대 배율 변경) 새 크기로 처음부터 다시 푼다."""
        source = job.source
        frame_count = job.frame_count
        generation = job.generation
        w, h = need.width(), need.height()
        self._first_loop_last_restart = time.perf_counter()
        self._cancel_first_loop_decode()
        if self.anim_cache_playing:
            self._stop_anim_cache_playback()
        # 크기가 섞인 프레임이 남지 않게 먼저 비운다 (그래야 아래에서 읽는 여유 메모리가 정확하다).
        self.animated_frame_cache.clear()
        # 새 크기에서도 루프 전체가 캐시에 들어가는지 (show_current_image 와 같은 기준).
        budget = animated_cache_budget_bytes(get_available_physical_memory(), 0)
        limit = animated_cache_frame_limit(frame_count, w, h, budget)
        self.animated_frame_cache_limit = limit
        if source is None or limit < frame_count:
            # 루프 전체가 안 들어가는 애니메이션은 원래도 캐시 재생을 쓰지 않는다
            # (캐시 재생을 쓰지 않는 상태로 만든다).
            self.anim_frame_delays = None
            self._first_loop_fallback()
            return
        if not self._start_first_loop_decode(source, frame_count, w, h, generation):
            self._first_loop_fallback()

    def _try_start_anim_cache_playback(self, frame_number):
        """Called after every QMovie-driven frame. Once the first loop has
        left every frame of the animation in animated_frame_cache, pauses
        QMovie and shows the cached frames from our own timer instead
        (_anim_cache_tick), timed by the durations read from the file.

        Why: QMovie decodes -- and, at a scaled size, resamples -- every
        frame on the GUI thread on every loop, and that result was already
        being thrown away in favor of the cached pixmap, so loops after the
        first were exactly as slow as the first. With the whole loop cached
        there is nothing left to decode.

        Only ever engages when the whole loop fits in the cache (see the
        budget in show_current_image) and anim_frame_delays is set (webp
        that loops forever); otherwise this returns False and QMovie keeps
        driving playback exactly as before. Returns True if it took over."""
        delays = self.anim_frame_delays
        total = self.prefetch_frame_count
        if (self.anim_cache_playing or not delays
                or not total or total <= 1 or len(delays) != total
                or not self.current_movie):
            return False
        # Only the last frame of a loop (or the first frame of the next
        # one, if the last frame's color pass finished asynchronously)
        # can be where the cache newly becomes complete, so a partly
        # filled cache costs one length compare per frame instead of a
        # full scan.
        if frame_number != total - 1 and frame_number != 0:
            return False
        cache = self.animated_frame_cache
        if len(cache) < total:
            return False
        if not all(self._animated_cache_key(i) in cache for i in range(total)):
            return False
        self.current_movie.setPaused(True)
        self.anim_cache_playing = True
        self.anim_cache_index = frame_number
        self._anim_cache_deadline = time.perf_counter()
        self._schedule_next_cached_frame()
        # The first loop is over and the GUI thread is free of decoding:
        # now is when preparing the neighbors starts (it was held back
        # while this animation filled its own cache).
        self._preload_neighbor_animations()
        return True

    def _schedule_next_cached_frame(self):
        # Deadlines accumulate from the frame durations rather than each
        # wait being measured from "now", so the time spent showing a frame
        # comes out of its own duration instead of adding to it.
        self._anim_cache_deadline += self.anim_frame_delays[self.anim_cache_index] / 1000.0
        wait_ms = int(round((self._anim_cache_deadline - time.perf_counter()) * 1000))
        if wait_ms < 0:
            # Running behind (showing a frame took longer than its own
            # duration): show the next one right away, but don't try to
            # catch up by racing through the frames after it.
            self._anim_cache_deadline = time.perf_counter()
            wait_ms = 0
        self.anim_cache_timer.start(wait_ms)

    def _anim_cache_tick(self):
        if not self.anim_cache_playing or not self.current_movie:
            return
        generation = self.current_movie_generation
        try:
            total = self.prefetch_frame_count
            frame = (self.anim_cache_index + 1) % total
            key = self._animated_cache_key(frame)
            pixmap = self.animated_frame_cache.get(key)
            if pixmap is None:
                if self._first_loop_job is not None:
                    # 첫 루프 병렬 디코딩이 아직 이 프레임까지 못 왔다: QMovie 로 넘기지 말고
                    # 조금 있다가 다시 본다. 기다린 시간만큼 뒤로 밀리도록 기준 시각도 지금으로 맞춘다.
                    self._anim_cache_deadline = time.perf_counter()
                    self.anim_cache_timer.start(8)
                    return
                self._leave_anim_cache_mode()
                return
            # Slideshow "loop" mode counts finished loops off QMovie's
            # frameChanged, which a paused movie no longer emits -- feed
            # the same handler from here instead. It may move on to the
            # next image (which stops this replay via stop_current_movie).
            self.on_gif_frame_changed(frame)
            if generation != self.current_movie_generation:
                return
            self.animated_frame_cache.move_to_end(key)
            self.anim_cache_index = frame
            self.current_movie_frame = frame
            self._show_animated_pixmap(pixmap)
            self._schedule_next_cached_frame()
        except Exception as e:
            print(f"[애니메이션 캐시 재생 오류] {e}")
            # A fault must not turn into leave/re-enter cycling: no more
            # cached replay for this animation.
            self.anim_frame_delays = None
            self._leave_anim_cache_mode()

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
                    if self.anim_cache_playing:
                        # QMovie is paused during cached replay, so its
                        # currentFrameNumber()/currentImage() are the frame
                        # it stopped on, not the one on screen. Re-show the
                        # cached frame that is, at the new size.
                        cached = self.animated_frame_cache.get(self._animated_cache_key(self.anim_cache_index))
                        if cached is not None:
                            self.current_movie_frame = self.anim_cache_index
                            self._show_animated_pixmap(cached)
                        else:
                            self._leave_anim_cache_mode()
                    else:
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
        # fit_to_window is changed by nothing else but this method (and
        # _zoom_at, which only ever clears it), so "switched to actual size and
        # then went back to fit by itself" can only be this being called a
        # second time. Inputs that arrive as a burst -- one tilt of the wheel
        # can come in as several wheel events, keys repeat, and a GUI thread
        # that was busy replays everything it had queued in one go -- must
        # count once. The gap is measured both on our own clock and on the
        # native timestamps of the input events, whichever is smaller: events
        # that were only *processed* late still carry the time they happened.
        now = time.monotonic()
        gap = now - self._last_toggle_attempt
        ts = self._input_ts_ms
        if ts is not None and self._last_toggle_attempt_ts is not None:
            gap_ts = ((ts - self._last_toggle_attempt_ts) & 0xFFFFFFFF) / 1000.0
            if gap_ts < 0x7FFFFFFF / 1000.0:
                gap = min(gap, gap_ts)
        self._last_toggle_attempt = now
        self._last_toggle_attempt_ts = ts
        if gap < TOGGLE_ACTUAL_SIZE_MIN_INTERVAL_S:
            return
        self.fit_to_window = not self.fit_to_window
        if self.fit_to_window:
            self.zoom_factor = 1.0
        else:
            # Any fit-to-window timer armed before this toggle (most
            # notably right after launch: window-geometry restoration
            # fires a resizeEvent that arms both of these before the user
            # has had a chance to do anything) is now moot. Both timers'
            # own handlers already re-check fit_to_window when they
            # fire/complete and bail out if it's since gone False, so
            # this isn't required for correctness -- but stopping them
            # here means that leftover work never runs at all instead of
            # computing a result that just gets thrown away, which rules
            # it out entirely as a contributor to only-the-first-time
            # flakiness right after launch.
            self._hq_resample_timer.stop()
            self._display_update_timer.stop()
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

    def _handle_tilt_wheel(self, event):
        dx = event.angleDelta().x()
        if dx == 0:
            return False
        # Accepted up front: QApplication::notify keeps handing a wheel event to
        # the next parent widget (whose own filter would run this again) until
        # it has been accepted, and the shortcut below can take a while.
        event.accept()
        # The mouse reports horizontal tilt with the opposite sign on this
        # device/event path. Map the physical direction to the UI name.
        button_text = 'Tilt Left' if dx > 0 else 'Tilt Right'
        try:
            ts = event.timestamp()
        except Exception:
            ts = None
        self._input_ts_ms = ts
        try:
            self.check_mouse_shortcut(button_text)
        finally:
            self._input_ts_ms = None
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
            try:
                self._input_ts_ms = event.timestamp()
            except Exception:
                self._input_ts_ms = None
            try:
                self.check_mouse_shortcut(button_text)
            finally:
                self._input_ts_ms = None
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
        # A neighbor animation being pre-decoded in the background would
        # otherwise keep the process alive until its whole decode finished.
        for cancel_event in list(self.anim_preload_inflight.values()):
            try:
                cancel_event.set()
            except Exception:
                pass
        # Stop creating new background work and release all worker threads.
        # This is important on Windows: ThreadPoolExecutor worker threads can
        # keep the process alive and retain large decoded images/ZIP handles.
        try:
            self.load_generation += 1
            self.loading_keys.clear()
            self.cache_manager.clear()
            self.current_pixmap = None
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

    single_app = SingleApplication()
    if single_app.is_running():
        # Hand the file to the instance that is already running and leave
        # without ever building a window.
        if len(sys.argv) > 1:
            single_app.send_message(sys.argv[1])
        sys.exit(0)
    single_app.start_server()
    viewer = ImageViewer()
    single_app.set_file_received_callback(viewer.load_path)
    # Show the (still empty) window first and open the file from the event
    # loop afterwards. Opening it before the first paint meant the directory
    # scan and the first decode all delayed the window appearing; this also
    # sizes the first image against the real, laid-out window.
    viewer.show()
    if len(sys.argv) > 1:
        path = sys.argv[1]
        QTimer.singleShot(0, lambda: viewer.load_path(path))
        warm_up_for_first_file(os.path.splitext(path)[1].lower())
    else:
        QTimer.singleShot(100, viewer.force_foreground)
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
