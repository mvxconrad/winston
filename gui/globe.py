"""Globe widget for RECON + Command tabs.

GPU-accelerated dot-matrix globe with real coastline polygons, city markers,
and orthographic projection. Uses a fragment shader with a 2D land-mask
texture for continent/ocean classification. Falls back to QPainter if
PyOpenGL is unavailable.

Features:
  - Slow tactical spin with scan line
  - 18 city markers with glow halos
  - Zoom-to-location with smooth lerp animation
  - Click-to-city detection (back-projects click → lat/lon → nearest marker)
  - city_clicked signal for RECON tab integration
"""
import math
import time

from PyQt6.QtCore import Qt, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QRadialGradient, QPen, QBrush,
    QSurfaceFormat,
)
from PyQt6.QtWidgets import QWidget, QSizePolicy

# ── OpenGL imports — GPU-accelerated rendering ──
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from OpenGL.GL import (
        glClear, glClearColor, glViewport, glEnable, glDisable, glBlendFunc,
        glGenVertexArrays, glBindVertexArray, glGenBuffers, glBindBuffer,
        glBufferData, glEnableVertexAttribArray, glVertexAttribPointer,
        glDrawArrays, glUseProgram,
        glGetUniformLocation, glUniform1f, glUniform2f, glUniform1i,
        glCreateShader, glShaderSource, glCompileShader, glGetShaderiv,
        glGetShaderInfoLog, glCreateProgram, glAttachShader, glLinkProgram,
        glGetProgramiv, glGetProgramInfoLog, glDeleteShader,
        GL_COLOR_BUFFER_BIT, GL_BLEND, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA,
        GL_FLOAT, GL_FALSE, GL_TRIANGLE_STRIP, GL_ARRAY_BUFFER,
        GL_STATIC_DRAW, GL_VERTEX_SHADER, GL_FRAGMENT_SHADER,
        GL_COMPILE_STATUS, GL_LINK_STATUS, GL_TRUE,
    )
    import numpy as np
    _HAS_OPENGL = True
except ImportError:
    _HAS_OPENGL = False

# ── Local palette + font helper ──
_GREEN = QColor(34, 197, 94)

_GLOBE_MONO = ["JetBrains Mono", "Cascadia Mono", "DejaVu Sans Mono",
               "Consolas", "Menlo", "monospace"]


def _mono(size=10):
    from PyQt6.QtGui import QFont
    f = QFont()
    f.setFamilies(_GLOBE_MONO)
    f.setPointSize(size)
    return f


# ── Dynamic base class ──
_WinstonCoreBase = QOpenGLWidget if _HAS_OPENGL else QWidget


# ──────────────── Coastline polygons ────────────────
# Simplified coastline polygons — real lat/lon coordinates for major
# landmasses.  Point-in-polygon tested once at init to build the
# dot-matrix sets.  ~24 polygons covering all continents + major islands.

_COASTLINE_POLYS = [
    # North America (mainland)
    [(49,-125),(55,-130),(58,-137),(60,-146),(64,-165),(71,-157),(72,-130),
     (71,-95),(68,-80),(60,-64),(52,-55),(47,-52),(44,-66),(41,-70),
     (35,-75),(30,-81),(25,-80),(26,-82),(30,-88),(29,-95),(26,-97),
     (20,-87),(16,-88),(16,-96),(20,-105),(23,-110),(32,-117),(34,-120),
     (40,-124),(48,-125),(49,-125)],
    # Central America
    [(20,-87),(15,-84),(10,-84),(8,-77),(10,-76),(14,-87),(17,-88),(20,-87)],
    # South America
    [(12,-72),(10,-75),(8,-77),(4,-77),(-2,-80),(-5,-81),(-5,-75),(0,-50),
     (-2,-42),(-8,-35),(-12,-38),(-23,-42),(-28,-49),(-34,-53),(-42,-65),
     (-46,-68),(-52,-70),(-55,-68),(-55,-64),(-50,-73),(-42,-73),(-38,-57),
     (-33,-52),(-18,-40),(-12,-37),(-5,-35),(2,-50),(7,-60),(10,-72),(12,-72)],
    # Europe
    [(36,-9),(37,-1),(43,3),(44,8),(48,5),(48,2),(50,-5),(52,-10),
     (54,-10),(58,-5),(61,5),(64,14),(68,16),(70,26),(70,30),
     (60,30),(57,24),(54,14),(52,10),(50,14),(48,17),(47,14),
     (44,12),(43,16),(42,17),(40,26),(38,26),(36,28),(35,25),
     (38,10),(36,-5),(36,-9)],
    # Africa
    [(35,-6),(37,10),(33,13),(31,32),(28,33),(22,36),(15,42),(12,44),
     (11,51),(2,42),(0,42),(-2,40),(-12,40),(-15,35),(-25,33),(-35,25),
     (-35,18),(-27,15),(-18,12),(-12,14),(-5,12),(0,10),(5,1),(5,-5),
     (5,-10),(7,-13),(15,-17),(20,-17),(25,-15),(30,-10),(35,-6)],
    # Asia (mainland)
    [(42,28),(42,40),(38,45),(40,50),(37,55),(25,58),(23,68),(20,73),
     (8,77),(1,104),(6,101),(8,98),(16,108),(22,106),(22,114),(30,122),
     (35,129),(40,130),(42,131),(46,140),(50,143),(55,137),(60,135),
     (63,143),(65,170),(68,180),(72,180),(72,120),(73,80),(73,60),
     (68,50),(55,40),(45,35),(42,28)],
    # India
    [(30,68),(28,72),(24,72),(22,68),(20,73),(16,74),(8,77),(10,80),
     (22,88),(27,88),(30,80),(30,68)],
    # SE Asia peninsula
    [(22,98),(20,93),(16,98),(10,99),(1,104),(6,101),(8,98),(16,108),
     (22,106),(22,98)],
    # Australia
    [(-12,130),(-12,137),(-17,146),(-22,150),(-28,153),(-35,151),
     (-38,145),(-37,140),(-35,136),(-32,133),(-23,114),(-15,129),
     (-12,130)],
    # Greenland
    [(60,-45),(65,-55),(70,-55),(76,-60),(80,-65),(83,-30),(81,-17),
     (77,-18),(72,-22),(65,-40),(60,-45)],
    # British Isles
    [(50,-6),(51,-3),(54,-3),(57,-6),(58,-5),(58,-3),(54,0),(51,1),(50,-6)],
    # Japan
    [(31,131),(33,130),(35,133),(36,137),(39,140),(42,141),(45,142),
     (44,145),(40,140),(36,140),(34,135),(31,131)],
    # Indonesia (Sumatra+Java)
    [(-6,95),(-6,106),(-8,110),(-8,115),(-7,112),(-6,106),(-2,100),
     (5,97),(5,95),(-2,99),(-6,95)],
    # Borneo
    [(7,117),(4,108),(1,109),(-3,110),(-4,116),(1,118),(4,118),(7,117)],
    # New Zealand
    [(-35,172),(-37,175),(-42,174),(-47,167),(-46,166),(-43,170),
     (-38,176),(-35,174),(-35,172)],
    # Madagascar
    [(-12,49),(-16,44),(-19,44),(-24,44),(-26,47),(-22,48),(-16,50),
     (-12,49)],
    # Antarctica
    [(-65,-60),(-68,-70),(-70,-100),(-72,-130),(-75,-170),(-78,180),
     (-77,150),(-72,130),(-70,100),(-68,70),(-65,30),(-65,-10),
     (-65,-60)],
    # Iceland
    [(64,-24),(64,-14),(66,-14),(66,-22),(64,-24)],
    # Philippines (Luzon)
    [(14,120),(18,121),(19,122),(16,122),(14,120)],
    # Papua New Guinea
    [(-2,141),(-6,141),(-8,147),(-6,155),(-5,152),(-3,145),(-2,141)],
    # Cuba
    [(20,-85),(22,-84),(23,-80),(21,-77),(20,-82),(20,-85)],
    # Scandinavia (supplement)
    [(60,5),(63,5),(66,14),(70,20),(70,30),(68,16),(64,14),(61,5),(60,5)],
]

# City markers — (lat, lon, 3-letter code)
_CITY_MARKERS = [
    (40.7, -74.0, "NYC"), (34.1, -118.2, "LAX"), (51.5, -0.1, "LON"),
    (48.9, 2.3, "PAR"), (35.7, 139.7, "TKY"), (31.2, 121.5, "SHA"),
    (19.1, 72.9, "MUM"), (-33.9, 151.2, "SYD"), (55.8, 37.6, "MOW"),
    (-23.5, -46.6, "SAO"), (30.0, 31.2, "CAI"), (-1.3, 36.8, "NBO"),
    (1.3, 103.8, "SIN"), (37.6, 127.0, "SEL"), (52.5, 13.4, "BER"),
    (25.3, 55.3, "DXB"), (39.9, 116.4, "BEI"), (41.0, 29.0, "IST"),
]


# ──────────────── Geometry helpers ────────────────

def _pip(lat, lon, poly):
    """Ray-casting point-in-polygon test."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-10) + xi):
            inside = not inside
        j = i
    return inside


def _is_land(lat, lon):
    """Test if a lat/lon coordinate is on land."""
    for poly in _COASTLINE_POLYS:
        if _pip(lat, lon, poly):
            return True
    return False


def _globe_project(lat_deg, lon_deg, rotation, cx, cy, r):
    """Orthographic projection: lat/lon -> screen (x, y, z).
    z > 0 means the point is on the visible hemisphere."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg) - rotation
    x = r * math.cos(lat) * math.sin(lon)
    y = -r * math.sin(lat)
    z = math.cos(lat) * math.cos(lon)
    return (cx + x, cy + y, z)


# ──────────────── GLSL shaders ────────────────

_CORE_VERT_SRC = """
#version 330 core
layout(location = 0) in vec2 aPos;
out vec2 vUV;
void main() {
    vUV = aPos * 0.5 + 0.5;
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

_GLOBE_FRAG_SRC = """
#version 330 core
in vec2 vUV;
out vec4 fragColor;

uniform vec2  uResolution;
uniform float uRotation;
uniform float uScanAngle;
uniform float uZoom;

// Land mask: 90x180 texture — each texel = 1.0 if land, 0.0 if ocean
uniform sampler2D uLandMask;

// City markers — up to 18 cities: (lat_rad, lon_rad) packed into uniforms
uniform vec2  uCities[18];
uniform int   uNumCities;

const float PI  = 3.14159265;
const float TAU = 6.28318530;
const float DEG2RAD = PI / 180.0;

const vec3 GREEN      = vec3(0.133, 0.773, 0.369);  // #22C55E
const vec3 OCEAN_DARK = vec3(0.012, 0.05, 0.03);    // deep dark green-black
const vec3 LAND_DIM   = vec3(0.04, 0.18, 0.08);     // dim land base
const vec3 LAND_BRIGHT= vec3(0.08, 0.35, 0.15);     // lit land

// Orthographic back-projection: screen -> (lat, lon, depth)
vec3 backProject(vec2 p, float r, float rot) {
    float nx = p.x / r;
    float ny = p.y / r;
    float r2 = nx * nx + ny * ny;
    if (r2 > 1.0) return vec3(0.0, 0.0, -1.0);
    float nz = sqrt(1.0 - r2);
    float lat = asin(ny);
    float lon = atan(nx, nz) + rot;
    return vec3(lat, lon, nz);
}

// Sample land mask texture (lat in [-PI/2, PI/2], lon in [-PI, PI])
float isLand(float lat, float lon) {
    float u = mod(lon / TAU + 0.5, 1.0);
    float v = lat / PI + 0.5;
    return texture(uLandMask, vec2(u, v)).r;
}

// Smooth land sampling — sample a small neighborhood for anti-aliased edges
float isLandSmooth(float lat, float lon) {
    float step = 0.5 * DEG2RAD;
    float sum = 0.0;
    sum += isLand(lat, lon) * 2.0;
    sum += isLand(lat + step, lon);
    sum += isLand(lat - step, lon);
    sum += isLand(lat, lon + step);
    sum += isLand(lat, lon - step);
    return sum / 6.0;
}

void main() {
    vec2 px = gl_FragCoord.xy;
    vec2 res = uResolution;
    vec2 center = res * 0.5;
    vec2 p = px - center;
    float dist = length(p);
    float minDim = min(res.x, res.y);
    float r = minDim * 0.42 * uZoom;

    float nd = dist / r;
    float totalAlpha = 0.0;
    vec3 totalColor = vec3(0.0);

    // Light direction — slightly above-left for natural 3D shading
    vec3 lightDir = normalize(vec3(-0.3, 0.4, 1.0));

    // ── Outer atmosphere glow ──
    float atmoR = r * 1.35;
    float atmoD = dist / atmoR;
    if (atmoD > 0.70 && atmoD < 1.0) {
        float glow = smoothstep(1.0, 0.82, atmoD) * smoothstep(0.70, 0.82, atmoD);
        float ga = glow * 0.12;
        totalColor = GREEN * 0.5;
        totalAlpha = ga;
    }

    // ── Globe body ──
    if (nd < 1.0) {
        vec3 proj = backProject(p, r, uRotation);
        float lat = proj.x;
        float lon = proj.y;
        float z = proj.z;

        if (z > 0.0) {
            // Normal vector for this point on the sphere
            vec3 normal = vec3(p.x / r, p.y / r, z);

            // Lambertian diffuse shading
            float diffuse = max(dot(normal, lightDir), 0.0);
            float ambient = 0.15;
            float light = ambient + diffuse * 0.85;

            // Fresnel-like rim brightening
            float rim = 1.0 - z;
            float rimGlow = pow(rim, 3.0) * 0.4;

            // Land vs ocean
            float land = isLandSmooth(lat, lon);

            vec3 surfaceColor;
            if (land > 0.5) {
                // Land: solid fill with shading
                float blend = smoothstep(0.4, 0.7, land);
                surfaceColor = mix(LAND_DIM, LAND_BRIGHT, blend) * light;
                // Slight specular on land
                vec3 halfVec = normalize(lightDir + vec3(0.0, 0.0, 1.0));
                float spec = pow(max(dot(normal, halfVec), 0.0), 24.0);
                surfaceColor += GREEN * spec * 0.15;
            } else {
                // Ocean: very dark with subtle depth shading
                surfaceColor = OCEAN_DARK * (light * 0.6 + 0.4);
                // Subtle grid lines on ocean for texture
                float gridStep = (6.0 / max(1.0, uZoom * 0.5)) * DEG2RAD;
                float latLine = abs(mod(lat + gridStep * 0.5, gridStep) - gridStep * 0.5);
                float lonLine = abs(mod(lon + gridStep * 0.5, gridStep) - gridStep * 0.5);
                float cosLat = max(cos(lat), 0.1);
                float lineW = 0.3 * DEG2RAD;
                float latGrid = 1.0 - smoothstep(0.0, lineW, latLine);
                float lonGrid = 1.0 - smoothstep(0.0, lineW * cosLat, lonLine * cosLat);
                float grid = max(latGrid, lonGrid) * 0.06 * z;
                surfaceColor += GREEN * grid;
            }

            // Rim glow — green edge light
            surfaceColor += GREEN * rimGlow;

            // Coastline edge highlight
            if (land > 0.15 && land < 0.85) {
                float edgeFactor = smoothstep(0.15, 0.35, land) * smoothstep(0.85, 0.65, land);
                surfaceColor += GREEN * edgeFactor * 0.25 * z;
            }

            totalColor = surfaceColor;
            // Edge anti-alias: smooth sphere boundary
            float edgeAA = smoothstep(1.0, 0.98, nd);
            totalAlpha = edgeAA * 0.95;

            // ── Scan line ──
            float scanLon = mod(uScanAngle, TAU) - PI;
            float dScan = mod(lon - scanLon + PI, TAU) - PI;
            if (abs(dScan) < 4.0 * DEG2RAD) {
                float fade = max(0.0, 1.0 - abs(dScan) / (4.0 * DEG2RAD));
                float sa = 0.2 * z * fade;
                totalColor += GREEN * sa;
            }

            // ── City markers ──
            for (int i = 0; i < uNumCities; i++) {
                float cLat = uCities[i].x;
                float cLon = uCities[i].y;
                float cx2 = r * cos(cLat) * sin(cLon - uRotation);
                float cy2 = -r * sin(cLat);
                float cz = cos(cLat) * cos(cLon - uRotation);
                if (cz <= 0.1) continue;

                vec2 cityScreen = center + vec2(cx2, cy2);
                float cd = length(px - cityScreen);

                // Outer glow
                if (cd < 8.0) {
                    float ha = 0.2 * cz * (1.0 - cd / 8.0);
                    totalColor += GREEN * ha;
                }
                // Bright core
                if (cd < 3.5) {
                    float ga = 0.8 * cz * (1.0 - cd / 3.5);
                    totalColor += GREEN * ga;
                    totalAlpha = max(totalAlpha, 1.0);
                }
                // White-hot center
                if (cd < 1.5) {
                    float wa = 0.5 * cz * (1.0 - cd / 1.5);
                    totalColor += vec3(wa);
                }
            }
        }
    }

    fragColor = vec4(totalColor, totalAlpha);
}
"""


# ──────────────── Shader helpers ────────────────

def _compile_shader(src, shader_type):
    """Compile a GLSL shader, raise on error."""
    shader = glCreateShader(shader_type)
    glShaderSource(shader, src)
    glCompileShader(shader)
    if glGetShaderiv(shader, GL_COMPILE_STATUS) != GL_TRUE:
        log = glGetShaderInfoLog(shader).decode()
        raise RuntimeError(f"Shader compile error:\n{log}")
    return shader


def _link_program(vert, frag):
    """Link vertex + fragment shaders into a program."""
    prog = glCreateProgram()
    glAttachShader(prog, vert)
    glAttachShader(prog, frag)
    glLinkProgram(prog)
    if glGetProgramiv(prog, GL_LINK_STATUS) != GL_TRUE:
        log = glGetProgramInfoLog(prog).decode()
        raise RuntimeError(f"Program link error:\n{log}")
    glDeleteShader(vert)
    glDeleteShader(frag)
    return prog


# ──────────────── GlobeWidget ────────────────

class GlobeWidget(_WinstonCoreBase):
    """GPU-accelerated dot-matrix globe with real coastline data.

    Uses a fragment shader with a 2D land-mask texture for
    continent/ocean classification. Falls back to QPainter if
    PyOpenGL is unavailable.

    GPU path: shader does orthographic back-projection per pixel,
    snaps to grid, samples land mask -> draws dots. ~10 uniforms/frame.

    Zoom-to-location: click a city marker or call zoom_to(lat, lon)
    to smoothly lerp the globe rotation and scale toward that city.
    Emits city_clicked(code) signal when a city marker is clicked.
    """

    SPIN_SPEED = 0.12  # rad/s — slow tactical spin
    GRID_STEP = 4      # degrees between grid points

    # Signal emitted when a city marker is clicked — carries the 3-letter code
    city_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        if _HAS_OPENGL:
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            fmt.setAlphaBufferSize(8)
            fmt.setSamples(4)

        super().__init__(parent)

        if _HAS_OPENGL:
            self.setFormat(fmt)

        self.setMinimumSize(120, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._rotation = 0.0
        self._scan_angle = 0.0
        self._gl_ready = False

        # Zoom state
        self._target_lat = None
        self._target_lon = None
        self._target_zoom = 1.0
        self._current_zoom = 1.0
        self._zoomed = False

        # Pre-compute land and ocean dot sets (for fallback + land mask)
        step = self.GRID_STEP
        self._land_pts = []
        self._ocean_pts = []
        for lat in range(-85, 86, step):
            for lon in range(-180, 180, step):
                if _is_land(lat, lon):
                    self._land_pts.append((lat, lon))
                else:
                    if (lat + lon) % (step * 2) == 0:
                        self._ocean_pts.append((lat, lon))

        # Build land mask: 360x180 image (1 degree per pixel)
        self._land_mask_w = 360
        self._land_mask_h = 180
        self._land_mask_data = None
        if _HAS_OPENGL:
            mask = np.zeros((self._land_mask_h, self._land_mask_w),
                            dtype=np.float32)
            for lat in range(-89, 90):
                for lon in range(-180, 180):
                    if _is_land(lat, lon):
                        # v = lat mapping: -90 -> row 0, +89 -> row 179
                        row = lat + 90
                        col = (lon + 180) % 360
                        if 0 <= row < 180 and 0 <= col < 360:
                            mask[row, col] = 1.0
            self._land_mask_data = mask

        # Pre-allocated colors (for fallback)
        self._rim_color = QColor(34, 197, 94, 70)
        self._rim_glow = QColor(34, 197, 94, 15)

        # Animation timer ~30fps
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    # ── Zoom API ──

    def zoom_to(self, lat, lon, zoom_level=2.0):
        """Set a target location + zoom. Globe lerps toward it."""
        self._target_lat = lat
        self._target_lon = lon
        self._target_zoom = zoom_level
        self._zoomed = True

    def reset_zoom(self):
        """Return to free-spin mode."""
        self._target_lat = None
        self._target_lon = None
        self._target_zoom = 1.0
        self._zoomed = False

    # ── Animation tick ──

    def _tick(self):
        if not self.isVisible():
            return
        dt = 0.033  # ~30fps

        if self._target_lon is not None:
            # Lerp rotation toward target longitude
            target_rot = -math.radians(self._target_lon)
            diff = target_rot - self._rotation
            # Wrap to [-pi, pi]
            diff = (diff + math.pi) % (2 * math.pi) - math.pi
            self._rotation += diff * min(1.0, dt * 3.0)
        else:
            self._rotation += self.SPIN_SPEED * dt

        # Lerp zoom
        self._current_zoom += (self._target_zoom - self._current_zoom) * min(1.0, dt * 4.0)

        self._scan_angle += 0.6 * dt
        self.update()

    # ── Mouse click → city detection ──

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Find nearest city marker to click position
            x, y = event.position().x(), event.position().y()
            cx, cy = self.width() / 2, self.height() / 2
            r = min(self.width(), self.height()) * 0.42 * self._current_zoom

            best_dist = float('inf')
            best_city = None
            for clat, clon, code in _CITY_MARKERS:
                sx, sy, z = _globe_project(clat, clon, self._rotation, cx, cy, r)
                if z <= 0.1:
                    continue
                d = math.hypot(x - sx, y - sy)
                if d < best_dist and d < 30:  # 30px click radius
                    best_dist = d
                    best_city = (clat, clon, code)

            if best_city:
                self.zoom_to(best_city[0], best_city[1])
                self.city_clicked.emit(best_city[2])
            elif self._zoomed:
                self.reset_zoom()

    # ── OpenGL setup ──

    def initializeGL(self):
        if not _HAS_OPENGL:
            return
        try:
            from OpenGL.GL import (
                glGenTextures, glBindTexture, glTexImage2D, glTexParameteri,
                GL_TEXTURE_2D, GL_RED, GL_R32F, GL_TEXTURE_MIN_FILTER,
                GL_TEXTURE_MAG_FILTER, GL_LINEAR, GL_TEXTURE_WRAP_S,
                GL_TEXTURE_WRAP_T, GL_REPEAT,
            )

            vert = _compile_shader(_CORE_VERT_SRC, GL_VERTEX_SHADER)
            frag = _compile_shader(_GLOBE_FRAG_SRC, GL_FRAGMENT_SHADER)
            self._program = _link_program(vert, frag)

            # Uniform locations
            self._u_resolution = glGetUniformLocation(self._program, "uResolution")
            self._u_rotation   = glGetUniformLocation(self._program, "uRotation")
            self._u_scan_angle = glGetUniformLocation(self._program, "uScanAngle")
            self._u_num_cities = glGetUniformLocation(self._program, "uNumCities")
            self._u_zoom       = glGetUniformLocation(self._program, "uZoom")

            # City uniform locations
            self._u_cities = []
            for i in range(18):
                loc = glGetUniformLocation(self._program, f"uCities[{i}]")
                self._u_cities.append(loc)

            # Fullscreen quad VAO
            quad = np.array([
                -1.0, -1.0,  1.0, -1.0,  -1.0, 1.0,  1.0, 1.0,
            ], dtype=np.float32)
            self._vao = glGenVertexArrays(1)
            glBindVertexArray(self._vao)
            vbo = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBufferData(GL_ARRAY_BUFFER, quad.nbytes, quad, GL_STATIC_DRAW)
            glEnableVertexAttribArray(0)
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, None)
            glBindVertexArray(0)

            # Land mask texture
            self._land_tex = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, self._land_tex)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_R32F,
                         self._land_mask_w, self._land_mask_h, 0,
                         GL_RED, GL_FLOAT, self._land_mask_data)

            # Store GL constants for use in paintGL
            self._GL_TEXTURE_2D = GL_TEXTURE_2D

            self._gl_ready = True
        except Exception as e:
            print(f"[GlobeWidget] OpenGL init failed, using QPainter: {e}")
            self._gl_ready = False

    def paintGL(self):
        if not self._gl_ready:
            self._paint_fallback()
            return

        from OpenGL.GL import (
            glActiveTexture, glBindTexture, GL_TEXTURE0, GL_TEXTURE_2D,
        )

        w = int(self.width() * self.devicePixelRatioF())
        h = int(self.height() * self.devicePixelRatioF())
        if w < 20 or h < 20:
            return

        glViewport(0, 0, w, h)
        glClearColor(0.0, 0.0, 0.0, 0.0)
        glClear(GL_COLOR_BUFFER_BIT)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        glUseProgram(self._program)
        glUniform2f(self._u_resolution, float(w), float(h))
        glUniform1f(self._u_rotation, self._rotation)
        glUniform1f(self._u_scan_angle, self._scan_angle)
        glUniform1f(self._u_zoom, self._current_zoom)

        # Upload city positions as radians
        num_cities = min(len(_CITY_MARKERS), 18)
        glUniform1i(self._u_num_cities, num_cities)
        for i in range(num_cities):
            clat, clon, _ = _CITY_MARKERS[i]
            glUniform2f(self._u_cities[i],
                        math.radians(clat), math.radians(clon))

        # Bind land mask texture
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self._land_tex)

        glBindVertexArray(self._vao)
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        glBindVertexArray(0)

        glUseProgram(0)
        glDisable(GL_BLEND)

    # ── QPainter fallback ──

    def _paint_fallback(self):
        w, h = self.width(), self.height()
        if w < 20 or h < 20:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx, cy = w * 0.5, h * 0.5
        r = min(w, h) * 0.42 * self._current_zoom
        rot = self._rotation

        # Atmosphere glow
        atmo = QRadialGradient(QPointF(cx, cy), r * 1.25)
        atmo.setColorAt(0.0, QColor(34, 197, 94, 0))
        atmo.setColorAt(0.75, QColor(34, 197, 94, 0))
        atmo.setColorAt(0.88, self._rim_glow)
        atmo.setColorAt(1.0, QColor(34, 197, 94, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(atmo))
        painter.drawEllipse(QPointF(cx, cy), r * 1.25, r * 1.25)

        # Rim circle
        painter.setPen(QPen(self._rim_color, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # Ocean dots
        painter.setPen(Qt.PenStyle.NoPen)
        for lat, lon in self._ocean_pts:
            sx, sy, z = _globe_project(lat, lon, rot, cx, cy, r)
            if z <= 0.02:
                continue
            a = int(18 * z)
            painter.setBrush(QBrush(QColor(34, 197, 94, max(5, a))))
            painter.drawEllipse(QPointF(sx, sy), 0.8, 0.8)

        # Land dots
        for lat, lon in self._land_pts:
            sx, sy, z = _globe_project(lat, lon, rot, cx, cy, r)
            if z <= 0.02:
                continue
            a = int(100 + 120 * z)
            dot_r = 1.2 + 0.6 * z
            painter.setBrush(QBrush(QColor(34, 197, 94, min(255, a))))
            painter.drawEllipse(QPointF(sx, sy), dot_r, dot_r)

        # City markers
        font_city = _mono(5)
        painter.setFont(font_city)
        for clat, clon, code in _CITY_MARKERS:
            sx, sy, z = _globe_project(clat, clon, rot, cx, cy, r)
            if z <= 0.15:
                continue
            ga = int(180 * z)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(34, 197, 94, min(255, ga))))
            painter.drawEllipse(QPointF(sx, sy), 2.5, 2.5)
            painter.setBrush(QBrush(QColor(34, 197, 94, int(40 * z))))
            painter.drawEllipse(QPointF(sx, sy), 5.0, 5.0)
            if z > 0.5:
                painter.setPen(QPen(QColor(34, 197, 94, int(140 * z))))
                painter.drawText(int(sx + 4), int(sy - 3), code)

        # Scan line
        scan_lon = math.degrees(self._scan_angle) % 360 - 180
        painter.setPen(Qt.PenStyle.NoPen)
        for lat in range(-80, 81, 3):
            for d_lon in range(-3, 4, 2):
                slon = scan_lon + d_lon
                sx, sy, z = _globe_project(lat, slon, rot, cx, cy, r)
                if z <= 0.05:
                    continue
                fade = max(0.0, 1.0 - abs(d_lon) / 4.0)
                a = int(35 * z * fade)
                painter.setBrush(QBrush(QColor(34, 197, 94, max(3, a))))
                painter.drawEllipse(QPointF(sx, sy), 1.0, 1.0)

        painter.end()

    def paintEvent(self, event):
        if _HAS_OPENGL:
            super().paintEvent(event)
            return
        self._paint_fallback()
