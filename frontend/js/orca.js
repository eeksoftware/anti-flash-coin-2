/* Login stage. A parametric orca surface is sampled once and never drawn.
   Every motion after that — swim, gather, pointer, sonar, scan — runs in the
   vertex shader. The render loop only updates uniforms. */

import * as THREE from "../vendor/three.module.min.js";

/* All tuneable scene values live here.
   Pose and rig placement stay on the values that already sit the orca on the page.
   mix: share of the final point budget (surface / interior / edge aura).
   share: how the surface budget is split. Fin meshes are unchanged.
   sizes: point diameter in CSS pixels before device pixel ratio.
   ramp: brightness 0..1. White marks use the top, the body interior the bottom.
   light: fresnel rim and a key from the upper left. exposure keeps the body blue.
   bloom: half-resolution threshold blur. Core points stay sharp.
   pose: current resting yaw / pitch / roll, in radians. */
const CONFIG = {
  count: { desktop: 90000, mobile: 32000, plankton: 240, sparks: 36 },
  mix: { surface: 0.82, volume: 0.12, aura: 0.06 },
  share: { body: 0.635, dorsal: 0.154, far: 0.096, pectoral: 0.048, fluke: 0.067 },
  dprCap: 1.75,
  pointSize: 1.6,
  sizes: { surface: 3.05, edge: 2.35, mark: 2.0, volume: 1.15 },
  formSeconds: 2.8,
  scale: 1.2,
  diveMs: 900,
  fpsFloor: 30,
  frameBudgetMs: 22,
  camera: { fov: 34, z: 7.15 },
  pose: { yaw: 0.38, pitch: -0.04, roll: -0.04 },
  swim: { amp: 0.3, freq: 2.05, wave: 0.92, bob: 0.055, drift: 0.07 },
  mouse: { radius: 0.95, push: 0.55, headYaw: 0.1, parallaxDeg: 7, cardDamp: 0.16 },
  sonar: { interval: 6, speed: 1.45, width: 0.09 },
  scan: { interval: 7, duration: 2.2 },
  light: {
    rimPower: 2.4,
    rimStrength: 1.5,
    ambient: 0.18,
    key: 0.7,
    keyDir: [-0.55, 0.75, 0.35],
    exposure: 0.92,
  },
  life: { twinkle: 0.18, spark: 2.2, curl: 0.018 },
  aura: { offset: 0.04 },
  bloom: { strength: 0.62, radius: 0.45, threshold: 0.22, scale: 0.35 },
  noise: { low: 7, high: 21 },
  ramp: ["#05173F", "#0B3FB8", "#1F7BFF", "#3CC8FF", "#9BEBFF", "#F0FBFF"],
  colors: {
    ink: "#1F7BFF",
    snow: "#F0FBFF",
    cyan: "#3CC8FF",
    ice: "#9BEBFF",
    deep: "#05173F",
    low: "#0B3FB8",
    gold: "#e2c27a",
    alert: "#d45364",
  },
};

const BODY_X0 = -2.4;
const BODY_X1 = 1.25;
const TAIL_X = 1.55;

const RY = [0.095, 0.22, 0.32, 0.38, 0.36, 0.29, 0.2, 0.14, 0.1, 0.07, 0.05];
const RZ = [0.068, 0.14, 0.2, 0.23, 0.22, 0.17, 0.12, 0.07, 0.034, 0.018, 0.009];
const YC = [0.0, 0.02, 0.03, 0.02, 0.0, -0.02, -0.02, 0.0, 0.02, 0.03, 0.04];
const HEAD_CAP = 0.08;
const EYE_PATCH = { t: 0.19, theta: 0.3, ht: 0.05, hth: 0.3, tilt: 4 };
const WHITE_LIMIT = [0.48, 0.62, 0.72, 0.74, 0.66, 0.52, 0.4, 0.3, 0.22, 0.16, 0.12];

const INK = new THREE.Color(CONFIG.colors.ink);
const SNOW = new THREE.Color(CONFIG.colors.snow);
const SADDLE = new THREE.Color(0xb7c6d8);
const EYE = new THREE.Color(0x05070b);

const ORCA_VERTEX = /* glsl */ `
attribute vec3 aHome;
attribute vec3 aDust;
attribute vec3 aNormal;
attribute float aSeed;
attribute float aShade;
attribute float aRegion;
attribute float aEdge;
uniform float uTime;
uniform float uAmp;
uniform float uFreq;
uniform float uWave;
uniform float uForm;
uniform float uSwim;
uniform float uHeadYaw;
uniform float uPush;
uniform float uRadius;
uniform vec3 uMouse;
uniform float uFlinch;
uniform float uDive;
uniform float uSonarTime;
uniform vec3 uSonarOrigin;
uniform float uSonarSpeed;
uniform float uSonarWidth;
uniform float uSonarRed;
uniform float uScanX;
uniform float uScale;
uniform float uPixelRatio;
uniform float uPointSize;
uniform float uAlpha;
uniform vec3 uInk;
uniform vec3 uSnow;
uniform vec3 uCyan;
uniform vec3 uIce;
uniform vec3 uDeep;
uniform vec3 uLow;
uniform vec3 uAlert;
uniform vec3 uC0;
uniform vec3 uC1;
uniform vec3 uC2;
uniform vec3 uC3;
uniform vec3 uC4;
uniform vec3 uC5;
uniform float uRimPower;
uniform float uRimStrength;
uniform float uAmbient;
uniform float uKeyStrength;
uniform vec3 uKeyDir;
uniform float uExposure;
uniform float uTwinkle;
uniform float uSpark;
uniform float uCurlAmp;
uniform float uSizeSurface;
uniform float uSizeEdge;
uniform float uSizeMark;
uniform float uSizeVolume;
uniform float uDebugRegions;
varying vec4 vColor;

vec3 ramp(float t) {
  if (t < 0.28) return mix(uC0, uC1, t / 0.28);
  if (t < 0.50) return mix(uC1, uC2, (t - 0.28) / 0.22);
  if (t < 0.72) return mix(uC2, uC3, (t - 0.50) / 0.22);
  if (t < 0.88) return mix(uC3, uC4, (t - 0.72) / 0.16);
  return mix(uC4, uC5, clamp((t - 0.88) / 0.12, 0.0, 1.0));
}

float sway(float x) {
  float phase = uTime * uFreq - x * uWave;
  float along = clamp((x + 0.35) / 3.15, 0.0, 1.0);
  float d = uAmp * uSwim * along * along * sin(phase);
  if (x > 0.15) {
    float ped = clamp((x - 0.15) / 0.7, 0.0, 1.0);
    d += uAmp * uSwim * 0.28 * ped * sin(phase - 0.35);
  }
  if (x > 0.7) {
    float fl = clamp((x - 0.7) / 0.7, 0.0, 1.0);
    d += uAmp * uSwim * 0.72 * fl * sin(phase - 0.95);
  }
  if (x < 0.2) d -= uAmp * uSwim * 0.07 * sin(phase);
  if (x < -1.0) {
    float ht = clamp((-1.0 - x) / 1.55, 0.0, 1.0);
    d += uHeadYaw * ht * ht * uForm;
  }
  return d;
}

void main() {
  vec3 home = aHome;
  home.z += sway(home.x);
  float pectoral = step(1.5, aRegion) * step(aRegion, 2.5);
  home.y += sin(uTime * 1.35 + aSeed * 4.0) * 0.035 * uSwim * pectoral;
  float delay = aSeed * 0.36;
  float gather = clamp((uForm - delay) / 0.64, 0.0, 1.0);
  float ease = 1.0 - pow(1.0 - gather, 3.0);
  vec3 offset = aDust - home;
  float spin = (1.0 - ease) * (1.2 + aSeed * 2.4);
  float handed = step(0.5, aSeed) * 2.0 - 1.0;
  float cs = cos(spin * handed);
  float sn = sin(spin * handed);
  float spunY = offset.y * cs - offset.z * sn;
  float spunZ = offset.y * sn + offset.z * cs;
  offset.y = spunY + sin(gather * 3.14159) * (0.32 + aSeed * 0.4);
  offset.z = spunZ;
  offset.x += sin(gather * 6.28318 + aSeed * 11.0) * (1.0 - ease) * 0.28;
  offset += vec3(
    sin(uTime * 1.6 + aSeed * 28.0),
    cos(uTime * 1.3 + aSeed * 21.0),
    0.0
  ) * (1.0 - ease) * 0.07;
  vec3 p = home + offset * (1.0 - ease);
  p.z += uFlinch * sin(aSeed * 18.0) * 0.28;
  p.y -= uDive * uDive * 2.6;
  p.z += uDive * 0.8;
  vec2 delta = p.xy - uMouse.xy;
  float dist = length(delta) + 0.0001;
  float influence = smoothstep(uRadius, 0.0, dist) * uPush;
  p.xy += (delta / dist) * influence * (0.45 + aSeed * 0.35);
  float beam = smoothstep(0.22, 0.0, abs(home.x - uScanX));
  float fake = step(0.988, aSeed) * beam * uSwim;
  p += aNormal * fake * 0.4;
  float ct = uTime * 0.15;
  vec3 q = home * 3.0 + aSeed;
  p += vec3(
    cos(q.y + ct) - cos(q.z + ct * 1.3),
    cos(q.z + ct * 0.7) - cos(q.x + ct),
    cos(q.x + ct * 1.1) - cos(q.y + ct * 0.9)
  ) * uCurlAmp * uForm;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  float depth = max(0.45, -mv.z);
  gl_Position = projectionMatrix * mv;
  float halo = step(5.5, aRegion) * (1.0 - step(6.5, aRegion));
  float volume = step(6.5, aRegion);
  float pupil = step(4.5, aRegion) * (1.0 - step(5.5, aRegion));
  float eyeMark = step(3.5, aRegion) * (1.0 - step(4.5, aRegion));
  float fin = step(0.5, aRegion) * step(aRegion, 3.5);
  float mark = max(eyeMark, smoothstep(0.82, 0.98, aShade) * (1.0 - fin));
  float px = mix(uSizeSurface, uSizeEdge, clamp(aEdge, 0.0, 1.0));
  px = mix(px, uSizeVolume, volume);
  px = mix(px, uSizeMark, mark);
  px = mix(px, uSizeEdge * 1.35, halo);
  float atten = clamp(7.15 / depth, 0.62, 1.15);
  gl_PointSize = clamp(px * uPixelRatio * atten * mix(1.2, 1.0, ease), 1.2, 6.5);
  vec3 n = normalize(normalMatrix * aNormal);
  float fres = pow(1.0 - abs(dot(n, normalize(-mv.xyz))), uRimPower);
  float key = max(dot(n, normalize(normalMatrix * uKeyDir)), 0.0);
  float saddle = step(0.5, aRegion) * step(aRegion, 1.5) * smoothstep(-1.05, -0.42, home.x);
  float finLead = fin * fres;
  float lit = uAmbient + fres * uRimStrength * 0.16 + key * uKeyStrength;
  float shadeLift = smoothstep(0.22, 0.92, aShade) * 0.34;
  float t = clamp((lit + shadeLift) * uExposure + mark * 0.5 + finLead * 0.2 + halo * 0.28 - saddle * 0.1 - volume * 0.16, 0.0, 1.0);
  float hz = mix(0.6, 2.4, aSeed);
  float tw = sin(uTime * hz * 6.2831853 + aSeed * 48.0);
  t = clamp(t + tw * uTwinkle, 0.0, 1.0);
  float sparkle = step(0.975, aSeed) * smoothstep(0.78, 0.96, fract(aSeed * 13.0 + uTime * 0.37));
  t = clamp(t * mix(1.0, uSpark, sparkle), 0.0, 1.0);
  vec3 base = ramp(t);
  base = mix(base, uC4, beam * 0.55 * uSwim);
  float sonar = 0.0;
  if (uSonarTime >= 0.0 && uSonarTime < 1.7) {
    float radius = uSonarTime * uSonarSpeed;
    float ring = abs(length(p - uSonarOrigin) - radius);
    sonar = smoothstep(uSonarWidth, 0.0, ring) * (1.0 - uSonarTime / 1.7);
  }
  base = mix(base, mix(uCyan, uAlert, uSonarRed), sonar * 0.9);
  base = mix(base, uAlert, fake);
  base = mix(base, uSnow, eyeMark);
  base = mix(base, vec3(0.03, 0.05, 0.09), pupil);
  if (uDebugRegions > 0.5) {
    base = mix(vec3(0.12, 0.35, 0.95), vec3(0.95, 0.25, 0.3), step(0.5, aRegion));
    base = mix(base, vec3(0.2, 0.9, 0.45), step(1.5, aRegion));
    base = mix(base, vec3(0.95, 0.85, 0.2), step(2.5, aRegion));
    base = mix(base, vec3(0.95, 0.95, 0.95), eyeMark);
    base = mix(base, vec3(0.05, 0.05, 0.08), pupil);
    base = mix(base, vec3(0.4, 0.85, 1.0), halo);
    base = mix(base, vec3(0.45, 0.2, 0.85), volume);
  }
  float alpha = mix(0.8, 1.0, smoothstep(0.15, 0.9, aShade));
  alpha = mix(alpha, 0.32, volume);
  alpha = mix(alpha, 0.22, halo);
  alpha = mix(alpha, 0.96, max(eyeMark, pupil));
  alpha *= mix(0.42, 1.0, ease) * (1.0 - uDive);
  alpha += sonar * 0.28 + sparkle * 0.25 + beam * 0.12 * uSwim;
  vColor = vec4(base, clamp(alpha, 0.0, 1.0) * uAlpha);
}
`;

const ORCA_FRAGMENT = /* glsl */ `
varying vec4 vColor;

void main() {
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d) * 2.0;
  float a = exp(-r * r * 10.0);
  float core = exp(-r * r * 28.0);
  gl_FragColor = vec4(vColor.rgb + core * 0.16, vColor.a * a);
}
`;

function spline(knots, t) {
  const n = knots.length - 1;
  const x = Math.min(Math.max(t, 0), 1) * n;
  const i = Math.min(Math.floor(x), n - 1);
  const f = x - i;
  const p0 = knots[Math.max(i - 1, 0)];
  const p1 = knots[i];
  const p2 = knots[i + 1];
  const p3 = knots[Math.min(i + 2, n)];
  return 0.5 * (2 * p1 + (-p0 + p2) * f + (2 * p0 - 5 * p1 + 4 * p2 - p3) * f * f + (-p0 + 3 * p1 - 3 * p2 + p3) * f * f * f);
}

function smoothstep(edge0, edge1, x) {
  const t = Math.min(Math.max((x - edge0) / (edge1 - edge0), 0), 1);
  return t * t * (3 - 2 * t);
}

function bodyRound(t) {
  let round = 1;
  if (t < HEAD_CAP) round = Math.sqrt(1 - (1 - t / HEAD_CAP) ** 2);
  if (t > 0.9) round *= Math.cos(((t - 0.9) / 0.1) * Math.PI * 0.5);
  return Math.max(round, 0.002);
}

function bodyPoint(t, theta, lift) {
  const round = bodyRound(t);
  const ry = spline(RY, t) * round;
  const rz = spline(RZ, t) * round;
  const s = Math.sin(theta);
  const c = Math.cos(theta);
  const yy = s >= 0 ? ry * s : ry * 0.72 * s;
  return [
    BODY_X0 + (BODY_X1 - BODY_X0) * t,
    spline(YC, t) + yy + s * lift,
    rz * c + c * lift,
    s,
    c,
  ];
}

function buildBody() {
  const SEG = 240;
  const RING = 84;
  const positions = [];
  const colors = [];
  const indices = [];
  for (let i = 0; i <= SEG; i += 1) {
    const t = i / SEG;
    const x = BODY_X0 + (BODY_X1 - BODY_X0) * t;
    const round = bodyRound(t);
    const ry = spline(RY, t) * round;
    const rz = spline(RZ, t) * round;
    const yc = spline(YC, t);
    const limit = spline(WHITE_LIMIT, t);
    for (let j = 0; j <= RING; j += 1) {
      const theta = (j / RING) * Math.PI * 2 - Math.PI / 2;
      const s = Math.sin(theta);
      const c = Math.cos(theta);
      const yy = s >= 0 ? ry * s : ry * 0.72 * s;
      positions.push(x, yc + yy, rz * c);
      const phi = Math.acos(Math.min(1, Math.max(-1, -s)));
      let shade = 0.5 + 0.06 * (1 - smoothstep(1.15, 2.7, phi));
      const lateral = Math.exp(-((phi - 1.4) ** 2) / 0.32);
      shade = Math.max(shade, 0.7 * lateral);
      const belly = smoothstep(limit + 0.1, limit - 0.22, phi);
      shade = shade * (1 - belly) + 0.98 * belly;
      const patchPhi = Math.PI / 2 + EYE_PATCH.theta + (t - EYE_PATCH.t) * EYE_PATCH.tilt;
      const eye = ((t - EYE_PATCH.t) / EYE_PATCH.ht) ** 2 + ((phi - patchPhi) / EYE_PATCH.hth) ** 2;
      shade = Math.max(shade, smoothstep(1.0, 0.55, eye));
      colors.push(shade, shade, shade);
    }
  }
  for (let i = 0; i < SEG; i += 1) {
    for (let j = 0; j < RING; j += 1) {
      const a = i * (RING + 1) + j;
      const b = a + RING + 1;
      indices.push(a, b, a + 1, a + 1, b, b + 1);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  geometry.setIndex(indices);
  return weld(geometry);
}

function weld(geometry, precision = 1e-4) {
  const pos = geometry.attributes.position;
  const col = geometry.attributes.color;
  const map = new Map();
  const index = [];
  const outPos = [];
  const outCol = [];
  const src = geometry.index ? geometry.index.array : null;
  const count = src ? src.length : pos.count;
  for (let k = 0; k < count; k += 1) {
    const i = src ? src[k] : k;
    const x = pos.getX(i);
    const y = pos.getY(i);
    const z = pos.getZ(i);
    const key = `${Math.round(x / precision)}_${Math.round(y / precision)}_${Math.round(z / precision)}`;
    let id = map.get(key);
    if (id === undefined) {
      id = outPos.length / 3;
      map.set(key, id);
      outPos.push(x, y, z);
      if (col) outCol.push(col.getX(i), col.getY(i), col.getZ(i));
    }
    index.push(id);
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute("position", new THREE.Float32BufferAttribute(outPos, 3));
  if (col) out.setAttribute("color", new THREE.Float32BufferAttribute(outCol, 3));
  out.setIndex(index);
  out.computeVertexNormals();
  geometry.dispose();
  return out;
}

function fin(shape, depth, taper) {
  const raw = new THREE.ExtrudeGeometry(shape, {
    depth,
    bevelEnabled: true,
    bevelThickness: depth * 0.45,
    bevelSize: depth * 0.5,
    bevelSegments: 4,
    curveSegments: 26,
  });
  raw.computeBoundingBox();
  const box = raw.boundingBox;
  const zMid = (box.min.z + box.max.z) / 2;
  const pos = raw.attributes.position;
  for (let i = 0; i < pos.count; i += 1) {
    const h = taper(pos.getX(i), pos.getY(i));
    const scale = 1 - 0.72 * Math.min(Math.max(h, 0), 1);
    pos.setZ(i, (pos.getZ(i) - zMid) * scale);
  }
  return weld(raw);
}

function shadeByNormal(geometry, top, bottom) {
  const normal = geometry.attributes.normal;
  const colors = new Float32Array(normal.count * 3);
  const color = new THREE.Color();
  for (let i = 0; i < normal.count; i += 1) {
    const mix = smoothstep(-0.25, 0.25, normal.getY(i));
    color.copy(bottom).lerp(top, mix);
    colors[i * 3] = color.r;
    colors[i * 3 + 1] = color.g;
    colors[i * 3 + 2] = color.b;
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  return geometry;
}

function paint(geometry, color) {
  const count = geometry.attributes.position.count;
  const colors = new Float32Array(count * 3);
  for (let i = 0; i < count; i += 1) {
    colors[i * 3] = color.r;
    colors[i * 3 + 1] = color.g;
    colors[i * 3 + 2] = color.b;
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  return geometry;
}

function mirrored(geometry) {
  const out = geometry.clone();
  out.scale(1, 1, -1);
  const index = out.index.array;
  for (let i = 0; i < index.length; i += 3) {
    const keep = index[i + 1];
    index[i + 1] = index[i + 2];
    index[i + 2] = keep;
  }
  out.index.needsUpdate = true;
  out.computeVertexNormals();
  return out;
}

function encodeShade(geometry, fn) {
  const pos = geometry.attributes.position;
  const normal = geometry.attributes.normal;
  const colors = new Float32Array(pos.count * 3);
  for (let i = 0; i < pos.count; i += 1) {
    colors[i * 3] = fn(
      pos.getX(i), pos.getY(i), pos.getZ(i),
      normal.getX(i), normal.getY(i), normal.getZ(i),
    );
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  return geometry;
}

function dorsalFin() {
  const shape = new THREE.Shape();
  shape.moveTo(-0.21, 0.4);
  shape.bezierCurveTo(-0.16, 0.58, -0.08, 0.76, 0.0, 0.9);
  shape.bezierCurveTo(0.06, 1.04, 0.14, 1.1, 0.18, 1.02);
  shape.bezierCurveTo(0.24, 0.86, 0.22, 0.72, 0.28, 0.55);
  shape.bezierCurveTo(0.34, 0.44, 0.4, 0.34, 0.44, 0.28);
  shape.bezierCurveTo(0.18, 0.33, -0.02, 0.39, -0.21, 0.4);
  shape.closePath();
  const geometry = fin(shape, 0.04, (_x, y) => (y - 0.28) / 0.82);
  geometry.applyMatrix4(new THREE.Matrix4().makeTranslation(-1.0, -0.02, 0));
  geometry.computeVertexNormals();
  return encodeShade(geometry, (_x, y) => 0.42 + 0.28 * smoothstep(0.2, 0.85, y));
}

function pectoralFin() {
  const shape = new THREE.Shape();
  shape.moveTo(0.0, 0.05);
  shape.bezierCurveTo(0.2, 0.14, 0.48, 0.12, 0.72, 0.01);
  shape.bezierCurveTo(0.78, -0.05, 0.64, -0.12, 0.46, -0.12);
  shape.bezierCurveTo(0.18, -0.11, 0.02, -0.06, 0.0, -0.03);
  shape.closePath();
  const geometry = fin(shape, 0.028, (x) => Math.max(0, x) / 0.78);
  geometry.applyMatrix4(new THREE.Matrix4().makeRotationZ(THREE.MathUtils.degToRad(-32)));
  geometry.applyMatrix4(new THREE.Matrix4().makeRotationY(THREE.MathUtils.degToRad(16)));
  geometry.applyMatrix4(new THREE.Matrix4().makeTranslation(-1.48, -0.08, 0.02));
  geometry.computeVertexNormals();
  return encodeShade(geometry, (_x, _y, _z, _nx, ny) => (ny < -0.2 ? 0.72 : 0.46));
}

function farPectoral() {
  const shape = new THREE.Shape();
  shape.moveTo(0.0, 0.05);
  shape.bezierCurveTo(0.22, 0.16, 0.52, 0.14, 0.86, 0.02);
  shape.bezierCurveTo(0.94, -0.06, 0.76, -0.16, 0.54, -0.15);
  shape.bezierCurveTo(0.2, -0.13, 0.02, -0.06, 0.0, -0.04);
  shape.closePath();
  const geometry = fin(shape, 0.032, (x) => Math.max(0, x) / 0.94);
  geometry.applyMatrix4(new THREE.Matrix4().makeRotationZ(THREE.MathUtils.degToRad(-54)));
  geometry.applyMatrix4(new THREE.Matrix4().makeRotationY(THREE.MathUtils.degToRad(24)));
  geometry.applyMatrix4(new THREE.Matrix4().makeTranslation(-1.08, -0.1, -0.34));
  geometry.computeVertexNormals();
  return encodeShade(geometry, (_x, _y, _z, _nx, ny) => (ny < -0.15 ? 0.84 : 0.58));
}

function flukes() {
  const shape = new THREE.Shape();
  shape.moveTo(0.92, 0.0);
  shape.quadraticCurveTo(1.2, 0.22, 1.36, 0.5);
  shape.quadraticCurveTo(1.46, 0.54, 1.52, 0.42);
  shape.quadraticCurveTo(1.44, 0.16, 1.56, 0.02);
  shape.lineTo(1.38, 0.0);
  shape.lineTo(1.56, -0.02);
  shape.quadraticCurveTo(1.44, -0.16, 1.52, -0.42);
  shape.quadraticCurveTo(1.46, -0.54, 1.36, -0.5);
  shape.quadraticCurveTo(1.2, -0.22, 0.92, 0.0);
  shape.closePath();
  const geometry = fin(shape, 0.026, (_x, y) => Math.abs(y) / 0.54);
  geometry.applyMatrix4(new THREE.Matrix4().makeRotationX(-0.85));
  geometry.applyMatrix4(new THREE.Matrix4().makeTranslation(0, 0.02, 0));
  geometry.computeVertexNormals();
  return encodeShade(geometry, (_x, y, z) => {
    const edge = Math.min(1, Math.hypot(y - 0.02, z) / 0.4);
    return 0.4 + edge * 0.22;
  });
}

function eyes() {
  const t = 0.14;
  const x = BODY_X0 + (BODY_X1 - BODY_X0) * t;
  const z = spline(RZ, t) * 0.96;
  const y = spline(YC, t) + 0.06;
  const right = new THREE.SphereGeometry(0.045, 18, 14);
  right.translate(x, y, z);
  right.computeVertexNormals();
  const left = mirrored(right);
  return [paint(right, EYE), paint(left, EYE)];
}

function concat(list) {
  let vertexCount = 0;
  let indexCount = 0;
  for (const g of list) {
    vertexCount += g.attributes.position.count;
    indexCount += g.index.count;
  }
  const position = new Float32Array(vertexCount * 3);
  const normal = new Float32Array(vertexCount * 3);
  const color = new Float32Array(vertexCount * 3);
  const index = new Uint32Array(indexCount);
  let vOffset = 0;
  let iOffset = 0;
  for (const g of list) {
    position.set(g.attributes.position.array, vOffset * 3);
    normal.set(g.attributes.normal.array, vOffset * 3);
    color.set(g.attributes.color.array, vOffset * 3);
    const src = g.index.array;
    for (let i = 0; i < src.length; i += 1) index[iOffset + i] = src[i] + vOffset;
    vOffset += g.attributes.position.count;
    iOffset += src.length;
    g.dispose();
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute("position", new THREE.BufferAttribute(position, 3));
  out.setAttribute("normal", new THREE.BufferAttribute(normal, 3));
  out.setAttribute("color", new THREE.BufferAttribute(color, 3));
  out.setIndex(new THREE.BufferAttribute(index, 1));
  return out;
}

function noise3(x, y, z) {
  const ix = Math.floor(x);
  const iy = Math.floor(y);
  const iz = Math.floor(z);
  const fx = x - ix;
  const fy = y - iy;
  const fz = z - iz;
  const ux = fx * fx * (3 - 2 * fx);
  const uy = fy * fy * (3 - 2 * fy);
  const uz = fz * fz * (3 - 2 * fz);
  const h = (xi, yi, zi) => {
    const n = Math.sin((xi + 3) * 127.1 + (yi + 5) * 311.7 + (zi + 7) * 74.7) * 43758.5453123;
    return n - Math.floor(n);
  };
  const c000 = h(ix, iy, iz);
  const c100 = h(ix + 1, iy, iz);
  const c010 = h(ix, iy + 1, iz);
  const c110 = h(ix + 1, iy + 1, iz);
  const c001 = h(ix, iy, iz + 1);
  const c101 = h(ix + 1, iy, iz + 1);
  const c011 = h(ix, iy + 1, iz + 1);
  const c111 = h(ix + 1, iy + 1, iz + 1);
  const x00 = c000 + (c100 - c000) * ux;
  const x10 = c010 + (c110 - c010) * ux;
  const x01 = c001 + (c101 - c001) * ux;
  const x11 = c011 + (c111 - c011) * ux;
  const y0 = x00 + (x10 - x00) * uy;
  const y1 = x01 + (x11 - x01) * uy;
  return (y0 + (y1 - y0) * uz) * 2 - 1;
}

function filamentAccept(x, y, z) {
  const n = noise3(x * CONFIG.noise.low, y * CONFIG.noise.low, z * CONFIG.noise.low) * 0.72
    + noise3(x * CONFIG.noise.high, y * CONFIG.noise.high, z * CONFIG.noise.high) * 0.28;
  return smoothstep(0.25, 0.75, n * 0.5 + 0.5) * 0.7 + 0.3;
}

/* Area-weighted surface sampling. The mesh itself is never drawn.
   bias "edge" thickens the silhouette. bias "body" does that and keeps filament clusters. */
function sampleSurface(geometry, count, region, bias) {
  const pos = geometry.attributes.position.array;
  const nor = geometry.attributes.normal.array;
  const col = geometry.attributes.color.array;
  const idx = geometry.index.array;
  const triangles = idx.length / 3;
  const cumulative = new Float64Array(triangles);
  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const na = new THREE.Vector3();
  let total = 0;
  const edge = bias === "edge" || bias === "body";
  for (let t = 0; t < triangles; t += 1) {
    const i0 = idx[t * 3] * 3;
    const i1 = idx[t * 3 + 1] * 3;
    const i2 = idx[t * 3 + 2] * 3;
    a.fromArray(pos, i0);
    b.fromArray(pos, i1);
    c.fromArray(pos, i2);
    b.sub(a);
    c.sub(a);
    let area = b.cross(c).length() * 0.5;
    if (edge) {
      na.set(nor[i0] + nor[i1] + nor[i2], nor[i0 + 1] + nor[i1 + 1] + nor[i2 + 1], nor[i0 + 2] + nor[i1 + 2] + nor[i2 + 2]);
      if (na.lengthSq() > 1e-8) na.normalize();
      const facing = Math.min(1, Math.abs(na.x * 0.371 + na.z * 0.928));
      area *= 1 + 0.25 * (1 - facing);
    }
    total += area;
    cumulative[t] = total;
  }
  const home = new Float32Array(count * 3);
  const normal = new Float32Array(count * 3);
  const shade = new Float32Array(count);
  const regionArr = new Float32Array(count);
  let filled = 0;
  let tries = 0;
  const cap = bias === "body" ? count * 2 : count;
  while (filled < count && tries < cap && total > 0) {
    tries += 1;
    const r = Math.random() * total;
    let lo = 0;
    let hi = triangles - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (cumulative[mid] < r) lo = mid + 1;
      else hi = mid;
    }
    const i0 = idx[lo * 3] * 3;
    const i1 = idx[lo * 3 + 1] * 3;
    const i2 = idx[lo * 3 + 2] * 3;
    let u = Math.random();
    let v = Math.random();
    if (u + v > 1) {
      u = 1 - u;
      v = 1 - v;
    }
    const w = 1 - u - v;
    const x = pos[i0] * w + pos[i1] * u + pos[i2] * v;
    const y = pos[i0 + 1] * w + pos[i1 + 1] * u + pos[i2 + 1] * v;
    const z = pos[i0 + 2] * w + pos[i1 + 2] * u + pos[i2 + 2] * v;
    if (bias === "body" && tries < count * 2 && Math.random() > filamentAccept(x, y, z)) continue;
    const o = filled * 3;
    home[o] = x;
    home[o + 1] = y;
    home[o + 2] = z;
    normal[o] = nor[i0] * w + nor[i1] * u + nor[i2] * v;
    normal[o + 1] = nor[i0 + 1] * w + nor[i1 + 1] * u + nor[i2 + 1] * v;
    normal[o + 2] = nor[i0 + 2] * w + nor[i1 + 2] * u + nor[i2 + 2] * v;
    const g = col[i0] * w + col[i1] * u + col[i2] * v;
    shade[filled] = Math.min(Math.max(g, 0), 1);
    regionArr[filled] = region;
    filled += 1;
  }
  return {
    home: home.subarray(0, filled * 3),
    normal: normal.subarray(0, filled * 3),
    shade: shade.subarray(0, filled),
    region: regionArr.subarray(0, filled),
  };
}

let cloudDensity = 1;

function createBag() {
  return { home: [], normal: [], shade: [], region: [], dust: [], edge: [] };
}

function bagAdd(bag, x, y, z, nx, ny, nz, shade, region) {
  bag.home.push(x, y, z);
  bag.normal.push(nx, ny, nz);
  bag.shade.push(shade);
  bag.region.push(region);
  const facing = Math.min(1, Math.abs(nx * 0.371 + nz * 0.928));
  bag.edge.push(1 - facing);
  const spin = Math.random() * Math.PI * 2;
  const lift = (Math.random() - 0.5) * Math.PI * 0.9;
  const radius = 2.2 + Math.random() * 2.6;
  bag.dust.push(
    (Math.random() - 0.38) * 7.4,
    Math.sin(lift) * radius * 0.9,
    Math.cos(spin) * Math.cos(lift) * radius * 0.75,
  );
}

function addCloud(bag, sample, kind) {
  const n = sample.shade.length;
  for (let i = 0; i < n; i += 1) {
    const j = i * 3;
    const x = sample.home[j];
    const y = sample.home[j + 1];
    const z = sample.home[j + 2];
    const nx = sample.normal[j];
    const ny = sample.normal[j + 1];
    const nz = sample.normal[j + 2];
    const shade = sample.shade[i];
    const region = sample.region[i];
    const jitter = (Math.random() - 0.5) * 0.012;
    bagAdd(bag, x + nx * jitter, y + ny * jitter, z + nz * jitter, nx, ny, nz, shade, region);
    if (kind !== "body" && Math.random() < 0.15) {
      bagAdd(bag, x - nx * 0.012, y - ny * 0.012, z - nz * 0.012, nx, ny, nz, shade * 0.9, region);
    }
  }
}

function addVolumeAndAura(bag, sample, volumeTarget, auraTarget) {
  const n = sample.shade.length;
  if (!n) return;
  for (let i = 0; i < volumeTarget; i += 1) {
    const k = Math.floor(Math.random() * n);
    const j = k * 3;
    const y = sample.home[j + 1];
    const depth = Math.random() ** 0.58;
    bagAdd(
      bag,
      sample.home[j] + (Math.random() - 0.5) * 0.03,
      y + (0.03 - y) * depth * 0.84,
      sample.home[j + 2] * (1 - depth * 0.84),
      sample.normal[j], sample.normal[j + 1], sample.normal[j + 2],
      sample.shade[k] * 0.32,
      7,
    );
  }
  for (let i = 0; i < auraTarget; i += 1) {
    const k = Math.floor(Math.random() * n);
    const j = k * 3;
    const nx = sample.normal[j];
    const ny = sample.normal[j + 1];
    const nz = sample.normal[j + 2];
    const out = Math.random() * CONFIG.aura.offset;
    bagAdd(
      bag,
      sample.home[j] + nx * out,
      sample.home[j + 1] + ny * out,
      sample.home[j + 2] + nz * out,
      nx, ny, nz,
      0.72 + Math.random() * 0.2,
      6,
    );
  }
}

function stampJoints(bag) {
  const neck = Math.round(780 * cloudDensity);
  for (let i = 0; i < neck; i += 1) {
    const t = i / (neck - 1);
    const x = 0.55 + t * 0.7;
    const radius = 0.11 * (1 - t * 0.45);
    const angle = Math.random() * Math.PI * 2;
    const reach = Math.sqrt(Math.random()) * radius;
    bagAdd(bag, x, Math.cos(angle) * reach * 0.72 + 0.02, Math.sin(angle) * reach, 0, 0, 1, 0.46, 0);
  }
  const collar = Math.round(180 * cloudDensity);
  for (let i = 0; i < collar; i += 1) {
    const u = Math.random();
    const y = 0.37 - u * 0.11 + Math.random() * 0.03;
    bagAdd(bag, -1.21 + u * 0.65, y, (Math.random() - 0.5) * 0.03, 0, 1, 0, 0.44, 1);
  }
  for (const side of [1, -1]) {
    for (let i = 0; i < Math.round(140 * cloudDensity); i += 1) {
      const u = Math.random() * 0.16;
      const v = (Math.random() - 0.5) * 0.1;
      bagAdd(bag, -1.48 + u * 0.4, -0.06 + v, side * (0.08 + Math.random() * 0.08), 0, -0.4, side, 0.48, 2);
    }
  }
}

/* Patch and pupil are sampled on the body surface itself, so they wrap the head
   instead of reading as a flat card from the posed angle. */
function stampEyes(bag) {
  function patch(tc, thc, ht, hth, tilt, lift, shade, region, amount) {
    for (const side of [1, -1]) {
      for (let i = 0; i < amount; i += 1) {
        const a = Math.random() * Math.PI * 2;
        const r = Math.sqrt(Math.random());
        const du = Math.cos(a) * r * ht;
        const theta = thc + du * tilt + Math.sin(a) * r * hth;
        const [x, y, z, s, c] = bodyPoint(tc + du, theta, lift);
        bagAdd(bag, x, y, z * side, 0, s, c * side, shade, region);
      }
    }
  }
  const e = EYE_PATCH;
  patch(e.t, e.theta, e.ht, e.hth, e.tilt, 0.008, 1, 4, Math.round(340 * cloudDensity));
  patch(e.t - e.ht * 1.05, e.theta - 0.2, 0.007, 0.06, 0, 0.012, 0, 5, Math.round(22 * cloudDensity));
}

function packBag(bag) {
  return {
    count: bag.shade.length,
    home: Float32Array.from(bag.home),
    normal: Float32Array.from(bag.normal),
    shade: Float32Array.from(bag.shade),
    region: Float32Array.from(bag.region),
    dust: Float32Array.from(bag.dust),
    edge: Float32Array.from(bag.edge),
  };
}

function buildOrcaCloud(count) {
  cloudDensity = Math.min(4, Math.max(1, count / 18000));
  const pectoral = pectoralFin();
  const surface = Math.max(400, Math.round(count * CONFIG.mix.surface));
  const parts = [
    [buildBody(), 0, CONFIG.share.body, "body", "body"],
    [dorsalFin(), 1, CONFIG.share.dorsal, "fin", "edge"],
    [farPectoral(), 2, CONFIG.share.far, "fin", "edge"],
    [mirrored(pectoral), 2, CONFIG.share.pectoral, "fin", "edge"],
    [flukes(), 3, CONFIG.share.fluke, "fin", "edge"],
  ];
  const bag = createBag();
  let bodySample = null;
  parts.forEach(([geometry, region, share, kind, bias]) => {
    const sample = sampleSurface(geometry, Math.max(80, Math.round(surface * share)), region, bias);
    addCloud(bag, sample, kind);
    if (kind === "body") bodySample = sample;
    geometry.dispose();
  });
  if (bodySample) {
    addVolumeAndAura(
      bag,
      bodySample,
      Math.round(count * CONFIG.mix.volume),
      Math.round(count * CONFIG.mix.aura),
    );
  }
  stampEyes(bag);
  stampJoints(bag);
  return packBag(bag);
}

function pointsFromCloud(cloud, uniforms) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(cloud.home.slice(), 3));
  geometry.setAttribute("aHome", new THREE.BufferAttribute(cloud.home, 3));
  geometry.setAttribute("aDust", new THREE.BufferAttribute(cloud.dust, 3));
  geometry.setAttribute("aNormal", new THREE.BufferAttribute(cloud.normal, 3));
  geometry.setAttribute("aSeed", new THREE.BufferAttribute(Float32Array.from({ length: cloud.count }, () => Math.random()), 1));
  geometry.setAttribute("aShade", new THREE.BufferAttribute(cloud.shade, 1));
  geometry.setAttribute("aRegion", new THREE.BufferAttribute(cloud.region, 1));
  geometry.setAttribute("aEdge", new THREE.BufferAttribute(cloud.edge, 1));
  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: ORCA_VERTEX,
    fragmentShader: ORCA_FRAGMENT,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.NormalBlending,
  });
  material.toneMapped = false;
  const points = new THREE.Points(geometry, material);
  points.frustumCulled = false;
  return points;
}

const PLANKTON_VERTEX = /* glsl */ `
attribute float aSeed;
attribute float aSize;
uniform float uTime;
uniform float uScale;
uniform float uPixelRatio;
varying float vAlpha;
varying vec3 vTint;
void main() {
  vec3 p = position;
  float s = aSeed * 6.2831853;
  float drift = mix(0.01, 0.03, aSeed) * 3.65;
  p.x += sin(uTime * 0.15 + s) * drift;
  p.y += cos(uTime * 0.11 + s * 1.7) * drift * 0.65;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  float depth = max(0.5, -mv.z);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = clamp(aSize * uPixelRatio * clamp(7.2 / depth, 0.45, 1.3), 1.2, 40.0);
  vAlpha = mix(0.04, 0.22, fract(aSeed * 7.13));
  vTint = mix(vec3(0.122, 0.420, 1.0), vec3(0.180, 0.659, 1.0), aSeed);
}
`;

const PLANKTON_FRAGMENT = /* glsl */ `
varying float vAlpha;
varying vec3 vTint;
void main() {
  float r = length(gl_PointCoord - vec2(0.5));
  float a = smoothstep(0.5, 0.08, r);
  gl_FragColor = vec4(vTint, a * vAlpha);
}
`;

const SPARK_VERTEX = /* glsl */ `
attribute float aSeed;
uniform float uTime;
uniform float uPixelRatio;
varying float vAlpha;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = clamp(mix(1.0, 2.0, aSeed) * uPixelRatio, 1.2, 3.2);
  float blink = smoothstep(0.55, 0.92, fract(aSeed * 9.1 + uTime * mix(0.15, 0.45, aSeed)));
  vAlpha = blink;
}
`;

const SPARK_FRAGMENT = /* glsl */ `
varying float vAlpha;
void main() {
  float r = length(gl_PointCoord - vec2(0.5)) * 2.0;
  float a = exp(-r * r * 8.0) * vAlpha;
  gl_FragColor = vec4(0.72, 0.95, 1.0, a);
}
`;

function buildPlankton(count) {
  const position = new Float32Array(count * 3);
  const seed = new Float32Array(count);
  const size = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    const front = Math.random() < 0.15;
    position[i * 3] = (Math.random() - 0.5) * 7.2;
    position[i * 3 + 1] = (Math.random() - 0.5) * 3.4;
    position[i * 3 + 2] = front ? 0.35 + Math.random() * 0.9 : -0.35 - Math.random() * 2.2;
    seed[i] = Math.random();
    size[i] = front ? 16 + Math.random() * 24 : 8 + Math.random() * 18;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(position, 3));
  geometry.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
  geometry.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
  return { geometry };
}

function buildSparks(count) {
  const position = new Float32Array(count * 3);
  const seed = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    position[i * 3] = (Math.random() - 0.45) * 5.4;
    position[i * 3 + 1] = (Math.random() - 0.5) * 2.4;
    position[i * 3 + 2] = (Math.random() - 0.5) * 1.2;
    seed[i] = Math.random();
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(position, 3));
  geometry.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
  return geometry;
}

function makeUniforms() {
  const color = (hex) => new THREE.Color(hex);
  return {
    uTime: { value: 0 },
    uAmp: { value: CONFIG.swim.amp },
    uFreq: { value: CONFIG.swim.freq },
    uWave: { value: CONFIG.swim.wave },
    uForm: { value: 0 },
    uSwim: { value: 0 },
    uHeadYaw: { value: 0 },
    uPush: { value: 0 },
    uRadius: { value: CONFIG.mouse.radius },
    uMouse: { value: new THREE.Vector3(8, 8, 0) },
    uFlinch: { value: 0 },
    uDive: { value: 0 },
    uSonarTime: { value: -1 },
    uSonarOrigin: { value: new THREE.Vector3(-2.05, 0.12, 0.4) },
    uSonarSpeed: { value: CONFIG.sonar.speed },
    uSonarWidth: { value: CONFIG.sonar.width },
    uSonarRed: { value: 0 },
    uScanX: { value: -99 },
    uScale: { value: 400 },
    uPixelRatio: { value: 1 },
    uPointSize: { value: CONFIG.pointSize },
    uAlpha: { value: 1 },
    uInk: { value: color(CONFIG.colors.ink) },
    uSnow: { value: color(CONFIG.colors.snow) },
    uCyan: { value: color(CONFIG.colors.cyan) },
    uIce: { value: color(CONFIG.colors.ice) },
    uDeep: { value: color(CONFIG.colors.deep) },
    uLow: { value: color(CONFIG.colors.low) },
    uAlert: { value: color(CONFIG.colors.alert) },
    uC0: { value: color(CONFIG.ramp[0]) },
    uC1: { value: color(CONFIG.ramp[1]) },
    uC2: { value: color(CONFIG.ramp[2]) },
    uC3: { value: color(CONFIG.ramp[3]) },
    uC4: { value: color(CONFIG.ramp[4]) },
    uC5: { value: color(CONFIG.ramp[5]) },
    uRimPower: { value: CONFIG.light.rimPower },
    uRimStrength: { value: CONFIG.light.rimStrength },
    uAmbient: { value: CONFIG.light.ambient },
    uKeyStrength: { value: CONFIG.light.key },
    uKeyDir: { value: new THREE.Vector3(...CONFIG.light.keyDir) },
    uExposure: { value: CONFIG.light.exposure },
    uTwinkle: { value: CONFIG.life.twinkle },
    uSpark: { value: CONFIG.life.spark },
    uCurlAmp: { value: CONFIG.life.curl },
    uSizeSurface: { value: CONFIG.sizes.surface },
    uSizeEdge: { value: CONFIG.sizes.edge },
    uSizeMark: { value: CONFIG.sizes.mark },
    uSizeVolume: { value: CONFIG.sizes.volume },
    uDebugRegions: { value: 0 },
  };
}

function supportsWebGL() {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

function projectStill(canvas, cloud, yaw) {
  const width = Math.max(1, canvas.clientWidth);
  const height = Math.max(1, canvas.clientHeight);
  const ratio = Math.min(window.devicePixelRatio || 1, CONFIG.dprCap);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const scale = Math.min(width, height) * 0.2 * CONFIG.scale;
  const originX = width * 0.46;
  const originY = height * 0.64;
  const cos = Math.cos(yaw);
  const sin = Math.sin(yaw);
  for (let i = 0; i < cloud.count; i += 1) {
    const x = cloud.home[i * 3];
    const y = cloud.home[i * 3 + 1];
    const z = cloud.home[i * 3 + 2];
    const sx = originX + (x * cos + z * sin) * scale;
    const sy = originY - y * scale;
    const shade = cloud.shade[i];
    const region = cloud.region[i];
    let fill = "rgba(22,142,255,0.8)";
    if (region >= 4.5 && region < 5.5) fill = "rgba(8,14,24,0.95)";
    else if (region >= 3.5 && region < 4.5 || shade > 0.84) fill = "rgba(241,250,255,0.92)";
    else if (shade > 0.52) fill = "rgba(0,207,255,0.78)";
    else if (shade < 0.22) fill = "rgba(7,81,141,0.8)";
    ctx.fillStyle = fill;
    ctx.fillRect(sx, sy, 1.6, 1.6);
  }
}

function mountStill(stage, host) {
  stage.classList.add("is-flat");
  const canvas = document.createElement("canvas");
  canvas.className = "gate-canvas";
  canvas.setAttribute("aria-hidden", "true");
  host.prepend(canvas);
  const cloud = buildOrcaCloud(Math.min(4200, CONFIG.count.mobile));
  const paint = () => projectStill(canvas, cloud, 0.42);
  paint();
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(paint) : null;
  if (observer) observer.observe(host);
  else window.addEventListener("resize", paint);
  return {
    dispose() {
      if (observer) observer.disconnect();
      else window.removeEventListener("resize", paint);
      canvas.remove();
    },
    flinch() {},
    dive() {
      return Promise.resolve();
    },
  };
}

const BLOOM_VERT = /* glsl */ `
varying vec2 vUv;
void main() {
  vUv = position.xy * 0.5 + 0.5;
  gl_Position = vec4(position.xy, 0.0, 1.0);
}
`;

function createStageBloom(renderer) {
  const opts = {
    depthBuffer: false,
    stencilBuffer: false,
    magFilter: THREE.LinearFilter,
    minFilter: THREE.LinearFilter,
    format: THREE.RGBAFormat,
    type: THREE.UnsignedByteType,
  };
  const sceneRT = new THREE.WebGLRenderTarget(2, 2, opts);
  const halfA = new THREE.WebGLRenderTarget(2, 2, opts);
  const halfB = new THREE.WebGLRenderTarget(2, 2, opts);
  const quadScene = new THREE.Scene();
  const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2));
  quad.frustumCulled = false;
  quadScene.add(quad);
  const quadCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const extract = new THREE.ShaderMaterial({
    uniforms: { tDiffuse: { value: null }, uThreshold: { value: CONFIG.bloom.threshold } },
    vertexShader: BLOOM_VERT,
    fragmentShader: /* glsl */ `
      uniform sampler2D tDiffuse;
      uniform float uThreshold;
      varying vec2 vUv;
      void main() {
        vec4 c = texture2D(tDiffuse, vUv);
        float l = dot(c.rgb, vec3(0.2126, 0.7152, 0.0722));
        float k = smoothstep(uThreshold, uThreshold + 0.22, l);
        gl_FragColor = vec4(c.rgb * k, c.a * k);
      }
    `,
    depthTest: false,
    depthWrite: false,
  });
  const blur = new THREE.ShaderMaterial({
    uniforms: {
      tDiffuse: { value: null },
      uDir: { value: new THREE.Vector2(1, 0) },
      uRadius: { value: CONFIG.bloom.radius },
      uResolution: { value: new THREE.Vector2(2, 2) },
    },
    vertexShader: BLOOM_VERT,
    fragmentShader: /* glsl */ `
      uniform sampler2D tDiffuse;
      uniform vec2 uDir;
      uniform float uRadius;
      uniform vec2 uResolution;
      varying vec2 vUv;
      void main() {
        vec2 texel = uDir * (1.0 + uRadius * 5.0) / uResolution;
        vec4 sum = texture2D(tDiffuse, vUv) * 0.227027;
        sum += texture2D(tDiffuse, vUv + texel * 1.384) * 0.316216;
        sum += texture2D(tDiffuse, vUv - texel * 1.384) * 0.316216;
        sum += texture2D(tDiffuse, vUv + texel * 3.230) * 0.070270;
        sum += texture2D(tDiffuse, vUv - texel * 3.230) * 0.070270;
        gl_FragColor = sum;
      }
    `,
    depthTest: false,
    depthWrite: false,
  });
  const compose = new THREE.ShaderMaterial({
    uniforms: {
      tScene: { value: null },
      tBloom: { value: null },
      uStrength: { value: CONFIG.bloom.strength },
    },
    vertexShader: BLOOM_VERT,
    fragmentShader: /* glsl */ `
      uniform sampler2D tScene;
      uniform sampler2D tBloom;
      uniform float uStrength;
      varying vec2 vUv;
      void main() {
        vec4 base = texture2D(tScene, vUv);
        vec4 glow = texture2D(tBloom, vUv);
        gl_FragColor = vec4(base.rgb + glow.rgb * uStrength, clamp(base.a + glow.a * 0.2, 0.0, 1.0));
      }
    `,
    depthTest: false,
    depthWrite: false,
    toneMapped: false,
  });
  extract.toneMapped = false;
  blur.toneMapped = false;
  let enabled = true;
  let scale = CONFIG.bloom.scale;
  let width = 2;
  let height = 2;
  function paint(material, target) {
    quad.material = material;
    renderer.setRenderTarget(target);
    renderer.render(quadScene, quadCam);
  }
  return {
    setEnabled(value) { enabled = value; },
    setScale(value) {
      scale = value;
      this.setSize(width, height);
    },
    setSize(w, h) {
      width = Math.max(2, Math.floor(w));
      height = Math.max(2, Math.floor(h));
      sceneRT.setSize(width, height);
      const hw = Math.max(2, Math.floor(width * scale));
      const hh = Math.max(2, Math.floor(height * scale));
      halfA.setSize(hw, hh);
      halfB.setSize(hw, hh);
      blur.uniforms.uResolution.value.set(hw, hh);
    },
    render(scene, camera) {
      if (!enabled || CONFIG.bloom.strength <= 0.001) {
        renderer.setRenderTarget(null);
        renderer.render(scene, camera);
        return;
      }
      renderer.setRenderTarget(sceneRT);
      renderer.setClearColor(0x000000, 0);
      renderer.clear(true, true, true);
      renderer.render(scene, camera);
      extract.uniforms.tDiffuse.value = sceneRT.texture;
      extract.uniforms.uThreshold.value = CONFIG.bloom.threshold;
      paint(extract, halfA);
      blur.uniforms.uRadius.value = CONFIG.bloom.radius;
      blur.uniforms.tDiffuse.value = halfA.texture;
      blur.uniforms.uDir.value.set(1, 0);
      paint(blur, halfB);
      blur.uniforms.tDiffuse.value = halfB.texture;
      blur.uniforms.uDir.value.set(0, 1);
      paint(blur, halfA);
      compose.uniforms.tScene.value = sceneRT.texture;
      compose.uniforms.tBloom.value = halfA.texture;
      compose.uniforms.uStrength.value = CONFIG.bloom.strength;
      renderer.setRenderTarget(null);
      renderer.setClearColor(0x000000, 0);
      renderer.clear(true, true, true);
      paint(compose, null);
    },
    dispose() {
      sceneRT.dispose();
      halfA.dispose();
      halfB.dispose();
      extract.dispose();
      blur.dispose();
      compose.dispose();
      quad.geometry.dispose();
    },
  };
}

function debugRow(parent, labelText) {
  const row = document.createElement("label");
  row.style.display = "grid";
  row.style.gridTemplateColumns = "88px 1fr";
  row.style.gap = "8px";
  row.style.alignItems = "center";
  row.style.margin = "4px 0";
  const name = document.createElement("span");
  name.textContent = labelText;
  row.append(name);
  parent.append(row);
  return row;
}

function mountOrcaDebug(api) {
  const panel = document.createElement("div");
  panel.className = "gate-debug-panel";
  panel.setAttribute("aria-hidden", "true");
  panel.style.position = "absolute";
  panel.style.left = "16px";
  panel.style.bottom = "16px";
  panel.style.zIndex = "4";
  panel.style.width = "240px";
  panel.style.maxHeight = "46vh";
  panel.style.overflow = "auto";
  panel.style.padding = "10px 12px";
  panel.style.borderRadius = "12px";
  panel.style.background = "rgba(5, 12, 24, 0.88)";
  panel.style.color = "#d5e6ff";
  panel.style.font = "11px/1.3 ui-monospace, monospace";
  panel.style.pointerEvents = "auto";
  const title = document.createElement("p");
  title.textContent = "Orca";
  title.style.margin = "0 0 6px";
  panel.append(title);
  const addSlider = (label, read, write, min, max, step) => {
    const row = debugRow(panel, label);
    const input = document.createElement("input");
    input.type = "range";
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(read());
    input.addEventListener("input", () => write(Number(input.value)));
    row.append(input);
  };
  const u = api.uniforms;
  addSlider("nokta", () => {
    const geometry = api.points().geometry;
    const full = geometry.getAttribute("aHome").count;
    const range = geometry.drawRange.count;
    return !range || range > full ? full : range;
  }, (value) => {
    api.points().geometry.setDrawRange(0, Math.round(value));
  }, 8000, 110000, 1000);
  addSlider("yüzey", () => u.uSizeSurface.value, (value) => { u.uSizeSurface.value = value; }, 0.6, 6.5, 0.05);
  addSlider("kenar", () => u.uSizeEdge.value, (value) => { u.uSizeEdge.value = value; }, 0.6, 4.5, 0.05);
  addSlider("rim", () => u.uRimStrength.value, (value) => { u.uRimStrength.value = value; }, 0, 3, 0.05);
  addSlider("parıltı", () => u.uTwinkle.value, (value) => { u.uTwinkle.value = value; }, 0, 0.6, 0.01);
  addSlider("kıvılcım", () => u.uSpark.value, (value) => { u.uSpark.value = value; }, 1, 3, 0.05);
  addSlider("gürültü", () => u.uCurlAmp.value, (value) => { u.uCurlAmp.value = value; }, 0, 0.08, 0.002);
  addSlider("bloom", () => CONFIG.bloom.strength, (value) => { CONFIG.bloom.strength = value; }, 0, 1.8, 0.02);
  addSlider("bloom yarıçap", () => CONFIG.bloom.radius, (value) => { CONFIG.bloom.radius = value; }, 0, 1.4, 0.02);
  addSlider("bloom eşik", () => CONFIG.bloom.threshold, (value) => { CONFIG.bloom.threshold = value; }, 0, 1, 0.01);
  addSlider("yaw", () => CONFIG.pose.yaw, (value) => { CONFIG.pose.yaw = value; api.rig.rotation.y = value; }, -1.2, 1.2, 0.01);
  addSlider("pitch", () => CONFIG.pose.pitch, (value) => { CONFIG.pose.pitch = value; api.rig.rotation.x = value; }, -0.6, 0.6, 0.01);
  addSlider("roll", () => CONFIG.pose.roll, (value) => { CONFIG.pose.roll = value; api.rig.rotation.z = value; }, -0.6, 0.6, 0.01);
  addSlider("ölçek", () => CONFIG.scale, (value) => { CONFIG.scale = value; api.rig.scale.setScalar(value); }, 0.6, 1.8, 0.02);
  const regionRow = debugRow(panel, "bölgeler");
  const region = document.createElement("input");
  region.type = "checkbox";
  region.addEventListener("change", () => { u.uDebugRegions.value = region.checked ? 1 : 0; });
  regionRow.append(region);
  CONFIG.ramp.forEach((hex, index) => {
    const row = debugRow(panel, `rampa ${index}`);
    const input = document.createElement("input");
    input.type = "color";
    input.value = hex;
    input.addEventListener("input", () => {
      CONFIG.ramp[index] = input.value;
      u[`uC${index}`].value.set(input.value);
    });
    row.append(input);
  });
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "Config'i JSON olarak kopyala";
  copy.style.marginTop = "8px";
  copy.style.width = "100%";
  copy.addEventListener("click", () => {
    const payload = {
      count: CONFIG.count,
      sizes: CONFIG.sizes,
      light: CONFIG.light,
      life: CONFIG.life,
      bloom: CONFIG.bloom,
      pose: CONFIG.pose,
      scale: CONFIG.scale,
      ramp: CONFIG.ramp,
      noise: CONFIG.noise,
    };
    const text = JSON.stringify(payload, null, 2);
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text);
  });
  panel.append(copy);
  api.host.append(panel);
  return panel;
}

export function mountOrca(stage, pointerSource) {
  const host = pointerSource || stage;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!stage || !host || reduced || !supportsWebGL()) return stage && host ? mountStill(stage, host) : null;
  stage.classList.remove("is-flat");
  const narrow = window.innerWidth < 760;
  const lowPower = narrow || (navigator.hardwareConcurrency || 8) <= 4;
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  } catch {
    return mountStill(stage, host);
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, CONFIG.dprCap));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = "gate-canvas";
  canvas.setAttribute("aria-hidden", "true");
  host.prepend(canvas);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(CONFIG.camera.fov, 1, 0.1, 60);
  const cameraBase = new THREE.Vector3(0.15, 0.02, CONFIG.camera.z);
  const lookAt = new THREE.Vector3(0.25, -0.12, 0);
  camera.position.copy(cameraBase);
  camera.lookAt(lookAt);

  const orcaUniforms = makeUniforms();
  let budget = lowPower ? CONFIG.count.mobile : CONFIG.count.desktop;
  const cloud = buildOrcaCloud(budget);
  let orcaPoints = pointsFromCloud(cloud, orcaUniforms);
  let orcaCount = orcaPoints.geometry.getAttribute("aHome").count;
  const rig = new THREE.Group();
  rig.add(orcaPoints);
  const baseYaw = CONFIG.pose.yaw;
  const baseRoll = CONFIG.pose.roll;
  rig.rotation.set(CONFIG.pose.pitch, baseYaw, baseRoll);
  rig.position.set(0.08, -0.52, 0);
  rig.scale.setScalar(CONFIG.scale);
  scene.add(rig);
  orcaPoints.renderOrder = 2;

  const plankton = buildPlankton(lowPower ? 80 : CONFIG.count.plankton);
  const planktonUniforms = {
    uTime: { value: 0 },
    uScale: { value: 400 },
    uPixelRatio: { value: renderer.getPixelRatio() },
  };
  const planktonPoints = new THREE.Points(plankton.geometry, new THREE.ShaderMaterial({
    uniforms: planktonUniforms,
    vertexShader: PLANKTON_VERTEX,
    fragmentShader: PLANKTON_FRAGMENT,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
  }));
  planktonPoints.frustumCulled = false;
  planktonPoints.position.copy(rig.position);
  planktonPoints.renderOrder = 0;
  scene.add(planktonPoints);
  const sparkUniforms = { uTime: { value: 0 }, uPixelRatio: { value: renderer.getPixelRatio() } };
  const sparkPoints = new THREE.Points(buildSparks(lowPower ? 16 : CONFIG.count.sparks), new THREE.ShaderMaterial({
    uniforms: sparkUniforms,
    vertexShader: SPARK_VERTEX,
    fragmentShader: SPARK_FRAGMENT,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
  }));
  sparkPoints.frustumCulled = false;
  sparkPoints.material.toneMapped = false;
  rig.add(sparkPoints);
  const bloom = createStageBloom(renderer);
  bloom.setEnabled(!lowPower);
  const debugOn = new URLSearchParams(window.location.search).get("debug") === "1";
  let panel = null;
  const debug = debugOn ? document.createElement("p") : null;
  if (debug) {
    debug.className = "gate-debug";
    debug.setAttribute("aria-hidden", "true");
    host.appendChild(debug);
  }
  if (debugOn) {
    panel = mountOrcaDebug({
      host,
      uniforms: orcaUniforms,
      bloom,
      rig,
      points: () => orcaPoints,
    });
  }

  const pointer = { x: 0, y: 0 };
  const target = { x: 0, y: 0, damp: 0 };
  let pointerSeen = false;
  let follow = 0;
  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const inverse = new THREE.Matrix4();
  const localOrigin = new THREE.Vector3();
  const localDir = new THREE.Vector3();
  const mouseLocal = new THREE.Vector3(8, 8, 0);
  const head = new THREE.Vector3(-2.05, 0.12, 0.42);
  let push = 0;
  let flinch = 0;
  let sonarRed = 0;
  let sonarClock = -1;
  let nextSonar = CONFIG.formSeconds + 0.6;
  let diveStart = 0;
  let running = true;
  let frame = 0;
  let start = performance.now();
  let last = start;
  let pausedAt = 0;
  let fpsStamp = start;
  let fpsFrames = 0;
  let fps = 60;
  let trimmed = false;
  const frameTimes = [];
  const rigHome = { x: rig.position.x, y: rig.position.y };
  let aspect = 1;

  function resize() {
    const width = Math.max(1, host.clientWidth);
    const height = Math.max(1, host.clientHeight);
    aspect = width / height;
    camera.aspect = aspect;
    const stacked = aspect < 1.15;
    cameraBase.z = CONFIG.camera.z * (stacked ? Math.min(1.75, 1.15 / Math.max(aspect, 0.55)) : 1);
    camera.position.z = cameraBase.z;
    rigHome.x = stacked ? -0.18 : 0.08;
    rigHome.y = stacked ? -1.25 : -0.52;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    const scale = height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
    const ratio = renderer.getPixelRatio();
    orcaUniforms.uScale.value = scale;
    orcaUniforms.uPixelRatio.value = ratio;
    planktonUniforms.uScale.value = scale;
    planktonUniforms.uPixelRatio.value = ratio;
    sparkUniforms.uPixelRatio.value = ratio;
    bloom.setSize(Math.floor(width * ratio), Math.floor(height * ratio));
  }

  function ping(origin, red) {
    sonarClock = (performance.now() - start) / 1000;
    orcaUniforms.uSonarOrigin.value.copy(origin);
    sonarRed = red ? 1 : 0;
    nextSonar = sonarClock + CONFIG.sonar.interval;
  }

  function replaceOrca(count) {
    const next = pointsFromCloud(buildOrcaCloud(count), orcaUniforms);
    const previous = orcaPoints.material;
    rig.remove(orcaPoints);
    orcaPoints.geometry.dispose();
    previous.dispose();
    orcaPoints = next;
    orcaPoints.renderOrder = 2;
    orcaCount = orcaPoints.geometry.getAttribute("aHome").count;
    rig.add(orcaPoints);
  }

  const source = host;
  function onMove(event) {
    pointerSeen = true;
    const rect = host.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    target.x = Math.min(Math.max(((event.clientX - rect.left) / rect.width) * 2 - 1, -1.4), 1.4);
    target.y = Math.min(Math.max(-(((event.clientY - rect.top) / rect.height) * 2 - 1), -1.2), 1.2);
    target.damp = 1;
  }
  function onLeave() {
    target.damp = 0;
  }
  function onDown(event) {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest(".gate-card, a, button, input, label")) return;
    onMove(event);
    pointerNdc.set(target.x, target.y);
    raycaster.setFromCamera(pointerNdc, camera);
    inverse.copy(rig.matrixWorld).invert();
    localOrigin.copy(raycaster.ray.origin).applyMatrix4(inverse);
    localDir.copy(raycaster.ray.direction).transformDirection(inverse);
    if (Math.abs(localDir.z) < 1e-4) return;
    const hit = -localOrigin.z / localDir.z;
    mouseLocal.set(localOrigin.x + localDir.x * hit, localOrigin.y + localDir.y * hit, 0);
    ping(mouseLocal, false);
  }
  function onVisibility() {
    if (document.hidden) {
      running = false;
      pausedAt = performance.now();
      cancelAnimationFrame(frame);
    } else if (!running && onScreen) {
      running = true;
      start += performance.now() - pausedAt;
      last = performance.now();
      frame = requestAnimationFrame(tick);
    }
  }

  source.addEventListener("pointermove", onMove, { passive: true });
  source.addEventListener("pointerleave", onLeave, { passive: true });
  source.addEventListener("pointerdown", onDown);
  document.addEventListener("visibilitychange", onVisibility);
  let onScreen = true;
  const viewObserver = typeof IntersectionObserver === "function"
    ? new IntersectionObserver((entries) => {
      onScreen = entries.some((entry) => entry.isIntersecting);
      if (!onScreen) {
        running = false;
        pausedAt = performance.now();
        cancelAnimationFrame(frame);
      } else if (!document.hidden && !running) {
        running = true;
        start += performance.now() - pausedAt;
        last = performance.now();
        frame = requestAnimationFrame(tick);
      }
    })
    : null;
  if (viewObserver) viewObserver.observe(host);
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
  if (observer) observer.observe(host);
  else window.addEventListener("resize", resize);
  resize();

  function tick(now) {
    if (!running) return;
    frame = requestAnimationFrame(tick);
    const t = (now - start) / 1000;
    const dt = Math.min(0.05, Math.max(0.001, (now - last) / 1000));
    last = now;
    if (pointerSeen) {
      pointer.x += (target.x - pointer.x) * 0.06;
      pointer.y += (target.y - pointer.y) * 0.06;
    }
    const damp = pointerSeen ? target.damp : 0;
    follow += (damp - follow) * 0.08;
    push += ((follow > 0.01 ? CONFIG.mouse.push * follow : 0) - push) * 0.08;
    flinch *= Math.exp(-dt * 7);
    const formRaw = Math.min(1, t / CONFIG.formSeconds);
    const form = formRaw;
    const swim = formRaw < 0.82 ? 0 : Math.min(1, (formRaw - 0.82) / 0.18);
    const parallax = THREE.MathUtils.degToRad(CONFIG.mouse.parallaxDeg) * follow;
    rig.rotation.y = baseYaw + pointer.x * parallax;
    rig.rotation.z = baseRoll - pointer.y * parallax * 0.45;
    rig.position.y = rigHome.y + Math.sin(t * 0.55) * CONFIG.swim.bob * swim;
    rig.position.x = rigHome.x + Math.sin(t * 0.12) * CONFIG.swim.drift * swim;
    rig.updateMatrixWorld();
    camera.position.x = cameraBase.x + pointer.x * 0.18 * follow;
    camera.position.y = cameraBase.y + pointer.y * 0.1 * follow;
    camera.lookAt(lookAt);

    if (pointerSeen) {
      pointerNdc.set(pointer.x, pointer.y);
      raycaster.setFromCamera(pointerNdc, camera);
      inverse.copy(rig.matrixWorld).invert();
      localOrigin.copy(raycaster.ray.origin).applyMatrix4(inverse);
      localDir.copy(raycaster.ray.direction).transformDirection(inverse);
      if (Math.abs(localDir.z) > 1e-4) {
        const hit = -localOrigin.z / localDir.z;
        if (hit > 0) mouseLocal.set(localOrigin.x + localDir.x * hit, localOrigin.y + localDir.y * hit, 0);
      }
    }

    if (t >= nextSonar && flinch < 0.08 && diveStart === 0) ping(head, false);
    const dive = diveStart ? Math.min(1, (now - diveStart) / CONFIG.diveMs) : 0;
    orcaUniforms.uTime.value = t;
    orcaUniforms.uForm.value = form;
    orcaUniforms.uSwim.value = swim;
    orcaUniforms.uHeadYaw.value = pointer.x * CONFIG.mouse.headYaw * follow * swim;
    orcaUniforms.uPush.value = push * swim;
    orcaUniforms.uMouse.value.copy(mouseLocal);
    orcaUniforms.uFlinch.value = flinch;
    orcaUniforms.uDive.value = dive;
    orcaUniforms.uSonarTime.value = sonarClock < 0 ? -1 : t - sonarClock;
    orcaUniforms.uSonarRed.value = sonarRed;
    const scanT = t - CONFIG.formSeconds;
    if (scanT > 0) {
      const period = CONFIG.scan.interval + CONFIG.scan.duration;
      const local = scanT % period;
      orcaUniforms.uScanX.value = local < CONFIG.scan.duration ? -2.8 + (5.6 * local) / CONFIG.scan.duration : -99;
    }
    planktonUniforms.uTime.value = t;
    sparkUniforms.uTime.value = t;
    bloom.render(scene, camera);

    frameTimes.push(dt * 1000);
    if (frameTimes.length > 120) frameTimes.shift();
    fpsFrames += 1;
    if (now - fpsStamp >= 1000) {
      fps = fpsFrames;
      fpsFrames = 0;
      fpsStamp = now;
      if (debug) debug.textContent = `${fps} fps · ${orcaCount} parçacık`;
      if (!trimmed && frameTimes.length === 120) {
        const average = frameTimes.reduce((sum, value) => sum + value, 0) / frameTimes.length;
        if (average > CONFIG.frameBudgetMs && orcaCount > CONFIG.count.mobile) {
          trimmed = true;
          const factor = Math.min(0.75, Math.max(0.45, CONFIG.frameBudgetMs / average));
          const next = Math.max(CONFIG.count.mobile, Math.round(orcaCount * factor));
          orcaPoints.geometry.setDrawRange(0, next);
          orcaCount = next;
          bloom.setScale(0.35);
          CONFIG.bloom.strength *= 0.7;
        }
      }
    }
  }
  frame = requestAnimationFrame(tick);

  return {
    flinch() {
      flinch = 1;
      ping(head, true);
    },
    dive() {
      diveStart = performance.now();
      return new Promise((resolve) => {
        window.setTimeout(resolve, CONFIG.diveMs);
      });
    },
    dispose() {
      running = false;
      cancelAnimationFrame(frame);
      if (observer) observer.disconnect();
      else window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", onVisibility);
      source.removeEventListener("pointermove", onMove);
      source.removeEventListener("pointerleave", onLeave);
      source.removeEventListener("pointerdown", onDown);
      if (viewObserver) viewObserver.disconnect();
      orcaPoints.geometry.dispose();
      orcaPoints.material.dispose();
      planktonPoints.geometry.dispose();
      planktonPoints.material.dispose();
      sparkPoints.geometry.dispose();
      sparkPoints.material.dispose();
      bloom.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
      if (debug) debug.remove();
      if (panel) panel.remove();
    },
  };
}
