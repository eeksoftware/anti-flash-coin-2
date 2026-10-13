/**
 * Login-card Rubik cube.
 * Piece positions and orientations stay on exact quarter-turn matrices.
 * Three.js only draws that model; it is loaded when the gate is shown.
 */

const FACES = ["U", "D", "R", "L", "F", "B"];
export const OPENING = ["R'", "U", "F'", "L", "D'", "B", "R", "U'"];
const MAX_AHEAD = 5;
const BURST = 3;

const TURNS = {
  U: { axis: "y", layer: 1, q: 1 },
  D: { axis: "y", layer: -1, q: -1 },
  R: { axis: "x", layer: 1, q: 1 },
  L: { axis: "x", layer: -1, q: -1 },
  F: { axis: "z", layer: 1, q: 1 },
  B: { axis: "z", layer: -1, q: -1 },
};

const ROT = {
  x: {
    1: [[1, 0, 0], [0, 0, -1], [0, 1, 0]],
    "-1": [[1, 0, 0], [0, 0, 1], [0, -1, 0]],
  },
  y: {
    1: [[0, 0, 1], [0, 1, 0], [-1, 0, 0]],
    "-1": [[0, 0, -1], [0, 1, 0], [1, 0, 0]],
  },
  z: {
    1: [[0, -1, 0], [1, 0, 0], [0, 0, 1]],
    "-1": [[0, 1, 0], [-1, 0, 0], [0, 0, 1]],
  },
};

const FACE_COLOR = {
  U: 0xf6f9ff,
  D: 0xffc42a,
  F: 0x1ad6ff,
  B: 0x1ed98a,
  R: 0xff4d67,
  L: 0xff8e22,
};

const PITCH = 1;
const CORE = 0.8;
const STICKER = 0.6;
const SIZE = 188;

function identity() {
  return [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
}

function mulMat(a, b) {
  const out = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let r = 0; r < 3; r += 1) {
    for (let c = 0; c < 3; c += 1) {
      out[r][c] = a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c];
    }
  }
  return out;
}

function mulVec(m, v) {
  return [
    m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
    m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
    m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
  ];
}

function parseMove(name) {
  const base = TURNS[name[0]];
  const q = name.endsWith("'") ? -base.q : base.q;
  return { axis: base.axis, layer: base.layer, q };
}

function invertMove(name) {
  return name.endsWith("'") ? name[0] : `${name[0]}'`;
}

function makeRng(seed) {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state;
  };
}

export function typingMoves(count) {
  const rng = makeRng(0x51f3c0de);
  const moves = [];
  let prev = OPENING[OPENING.length - 1][0];
  while (moves.length < count) {
    let next = FACES[rng() % FACES.length];
    if (next === prev) next = FACES[(FACES.indexOf(next) + 1) % FACES.length];
    prev = next;
    moves.push((rng() & 1) ? `${next}'` : next);
  }
  return moves;
}

export function createSolvedCubies() {
  const cubies = [];
  for (let x = -1; x <= 1; x += 1) {
    for (let y = -1; y <= 1; y += 1) {
      for (let z = -1; z <= 1; z += 1) {
        if (x === 0 && y === 0 && z === 0) continue;
        cubies.push({ x, y, z, hx: x, hy: y, hz: z, m: identity() });
      }
    }
  }
  return cubies;
}

export function applyMove(cubies, name) {
  const turn = parseMove(name);
  const spin = ROT[turn.axis][String(turn.q)];
  for (const cubie of cubies) {
    const coord = turn.axis === "x" ? cubie.x : turn.axis === "y" ? cubie.y : cubie.z;
    if (coord !== turn.layer) continue;
    const next = mulVec(spin, [cubie.x, cubie.y, cubie.z]);
    cubie.x = Math.round(next[0]);
    cubie.y = Math.round(next[1]);
    cubie.z = Math.round(next[2]);
    cubie.m = mulMat(spin, cubie.m);
  }
}

export function isHome(cubies) {
  return cubies.every((cubie) => (
    cubie.x === cubie.hx
    && cubie.y === cubie.hy
    && cubie.z === cubie.hz
    && cubie.m.every((row, r) => row.every((value, c) => value === (r === c ? 1 : 0)))
  ));
}

export function createMixedCube(moves = OPENING) {
  const cubies = createSolvedCubies();
  for (const name of moves) applyMove(cubies, name);
  return cubies;
}

function ease(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2;
}

function typedLength() {
  const user = document.getElementById("gate-user");
  const pass = document.getElementById("gate-pass");
  return (user ? user.value.length : 0) + (pass ? pass.value.length : 0);
}


function roundedSticker(THREE) {
  const shape = new THREE.Shape();
  const half = STICKER / 2;
  const radius = 0.07;
  shape.moveTo(-half + radius, -half);
  shape.lineTo(half - radius, -half);
  shape.absarc(half - radius, -half + radius, radius, -Math.PI / 2, 0, false);
  shape.lineTo(half, half - radius);
  shape.absarc(half - radius, half - radius, radius, 0, Math.PI / 2, false);
  shape.lineTo(-half + radius, half);
  shape.absarc(-half + radius, half - radius, radius, Math.PI / 2, Math.PI, false);
  shape.lineTo(-half, -half + radius);
  shape.absarc(-half + radius, -half + radius, radius, Math.PI, Math.PI * 1.5, false);
  return new THREE.ShapeGeometry(shape, 3);
}

export function mountLoginCube(root) {
  if (!root) return null;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const user = document.getElementById("gate-user");
  const pass = document.getElementById("gate-pass");
  const card = document.getElementById("gate-form");
  const cubies = createMixedCube();
  let disposed = false;
  let view = null;
  let pendingSolve = false;
  let pendingReject = false;
  let pendingComplete = null;

  const api = {
    solve() {
      if (disposed) return;
      pendingReject = false;
      if (!view) {
        pendingSolve = true;
        return;
      }
      view.solve();
    },
    reject() {
      if (disposed) return;
      pendingSolve = false;
      if (!view) {
        pendingReject = true;
        return;
      }
      view.reject();
    },
    complete() {
      if (disposed) return Promise.resolve();
      if (!view) {
        pendingSolve = false;
        pendingReject = false;
        return new Promise((resolve) => {
          pendingComplete = resolve;
        });
      }
      return view.complete();
    },
    sync() {
      if (disposed || !view) return;
      view.sync();
    },
    pause() {
      if (view) view.pause();
    },
    dispose() {
      disposed = true;
      if (view) view.dispose();
      view = null;
    },
  };

  import("../vendor/three.module.min.js").then((THREE) => {
    if (disposed) return;
    view = startView(THREE, root, cubies, { reduced, user, pass, card });
    if (pendingComplete) {
      const resolve = pendingComplete;
      pendingComplete = null;
      view.complete().then(resolve);
    } else if (pendingReject) view.reject();
    else if (pendingSolve) view.solve();
    else view.sync();
  }).catch(() => {
    /* The form stays usable when WebGL cannot start. */
  });

  return api;
}

function startView(THREE, root, cubies, opts) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  } catch {
    return {
      solve() {},
      reject() {},
      complete() { return Promise.resolve(); },
      sync() {},
      pause() {},
      dispose() {},
    };
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.setClearColor(0x000000, 0);
  renderer.setSize(SIZE, SIZE, false);
  const canvas = renderer.domElement;
  canvas.className = "gate-cube-canvas";
  canvas.setAttribute("aria-hidden", "true");
  while (root.firstChild) root.removeChild(root.firstChild);
  root.append(canvas);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(22, 1, 0.1, 40);
  camera.position.set(9.2, 6.6, 11.7);
  camera.lookAt(0, -0.28, 0);
  const group = new THREE.Group();
  group.scale.setScalar(1.32);
  scene.add(group);
  scene.add(new THREE.HemisphereLight(0xf7fbff, 0x1c2838, 0.72));
  const key = new THREE.DirectionalLight(0xfff6ea, 2.35);
  key.position.set(3.4, 6.4, 4.2);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x9aecff, 0.7);
  fill.position.set(-4.5, 1.4, 3.2);
  scene.add(fill);
  const rim = new THREE.DirectionalLight(0x7eb6ff, 0.85);
  rim.position.set(-1.5, -2.4, -4);
  scene.add(rim);
  const under = new THREE.DirectionalLight(0xe7f3ff, 0.7);
  under.position.set(1.2, -5.2, 4.2);
  scene.add(under);

  const coreGeo = new THREE.BoxGeometry(CORE, CORE, CORE);
  const stickerGeo = roundedSticker(THREE);
  const lift = CORE / 2 + 0.018;
  const stickerFaces = [
    { key: "R", home: "hx", sign: 1, pos: [lift, 0, 0], rot: [0, Math.PI / 2, 0] },
    { key: "L", home: "hx", sign: -1, pos: [-lift, 0, 0], rot: [0, -Math.PI / 2, 0] },
    { key: "U", home: "hy", sign: 1, pos: [0, lift, 0], rot: [-Math.PI / 2, 0, 0] },
    { key: "D", home: "hy", sign: -1, pos: [0, -lift, 0], rot: [Math.PI / 2, 0, 0] },
    { key: "F", home: "hz", sign: 1, pos: [0, 0, lift], rot: [0, 0, 0] },
    { key: "B", home: "hz", sign: -1, pos: [0, 0, -lift], rot: [0, Math.PI, 0] },
  ];
  const plastic = new THREE.MeshPhongMaterial({
    color: 0x1a2332,
    shininess: 48,
    specular: 0xa9c4e4,
  });
  const materials = { plastic };
  for (const [face, color] of Object.entries(FACE_COLOR)) {
    materials[face] = new THREE.MeshPhongMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.28,
      shininess: 90,
      specular: 0xffffff,
    });
  }

  const pieces = cubies.map((cubie) => {
    const piece = new THREE.Group();
    piece.matrixAutoUpdate = false;
    const body = new THREE.Mesh(coreGeo, plastic);
    piece.add(body);
    for (const face of stickerFaces) {
      if (cubie[face.home] !== face.sign) continue;
      const tile = new THREE.Mesh(stickerGeo, materials[face.key]);
      tile.position.set(face.pos[0], face.pos[1], face.pos[2]);
      tile.rotation.set(face.rot[0], face.rot[1], face.rot[2]);
      piece.add(tile);
    }
    group.add(piece);
    return piece;
  });

  const turnMat = new THREE.Matrix4();
  const pieceMat = new THREE.Matrix4();
  const pos = new THREE.Vector3();
  const axisVec = {
    x: new THREE.Vector3(1, 0, 0),
    y: new THREE.Vector3(0, 1, 0),
    z: new THREE.Vector3(0, 0, 1),
  };

  const moves = [];
  let display = 0;
  let target = 0;
  let finale = false;
  let rewinding = false;
  let bursting = false;
  let undoAfter = false;
  let held = null;
  let solving = false;
  let pendingComplete = false;
  let solveQueue = [];
  let onSolved = null;
  let turning = null;
  let raf = 0;
  let running = false;
  let dead = false;
  let locked = false;
  let settling = 0;
  const look = { x: 0, y: 0 };
  const smooth = { x: 0, y: 0 };

  function paint(turn) {
    cubies.forEach((cubie, index) => {
      const m = cubie.m;
      pieceMat.set(
        m[0][0], m[0][1], m[0][2], 0,
        m[1][0], m[1][1], m[1][2], 0,
        m[2][0], m[2][1], m[2][2], 0,
        0, 0, 0, 1,
      );
      pos.set(cubie.x, cubie.y, cubie.z);
      if (turn) {
        const coord = turn.axis === "x" ? cubie.x : turn.axis === "y" ? cubie.y : cubie.z;
        if (coord === turn.layer) {
          turnMat.makeRotationAxis(axisVec[turn.axis], turn.angle);
          pos.applyMatrix4(turnMat);
          pieceMat.premultiply(turnMat);
        }
      }
      pos.multiplyScalar(PITCH);
      pieceMat.setPosition(pos);
      pieces[index].matrix.copy(pieceMat);
      pieces[index].matrixWorldNeedsUpdate = true;
    });
  }

  function ensureMoves(count) {
    if (count <= moves.length) return;
    const planned = typingMoves(count);
    for (let index = moves.length; index < count; index += 1) moves.push(planned[index]);
  }

  function duration(gap) {
    if (opts.reduced) return 0;
    if (solving) return solveQueue.length > 4 ? 64 : 120;
    if (finale) return 78;
    if (rewinding) return 88;
    return Math.max(96, 220 - (gap - 1) * 28);
  }

  function drainBacklog() {
    const limit = finale || rewinding ? 8 : MAX_AHEAD;
    let jumped = false;
    while (Math.abs(target - display) > limit) {
      const forward = target > display;
      const name = forward ? moves[display] : invertMove(moves[display - 1]);
      applyMove(cubies, name);
      display += forward ? 1 : -1;
      jumped = true;
    }
    if (jumped) paint(null);
  }

  function beginComplete() {
    pendingComplete = false;
    undoAfter = false;
    rewinding = false;
    bursting = true;
    finale = true;
    locked = false;
    settling = 0;
    const inverse = [...OPENING, ...moves.slice(0, display)].reverse().map(invertMove);
    const tail = opts.reduced ? 0 : 10;
    const skip = Math.max(0, inverse.length - tail);
    for (let index = 0; index < skip; index += 1) applyMove(cubies, inverse[index]);
    solveQueue = inverse.slice(skip);
    solving = true;
    paint(null);
    render();
    kick();
  }

  function doneSolve() {
    solving = false;
    finale = false;
    locked = true;
    if (!opts.reduced) settling = performance.now();
    const resolve = onSolved;
    onSolved = null;
    if (resolve) setTimeout(resolve, opts.reduced ? 0 : 700);
  }

  function kick() {
    if (dead || turning) return;
    if (solving) {
      if (!solveQueue.length) {
        doneSolve();
        return;
      }
      const name = solveQueue[0];
      const spec = parseMove(name);
      if (opts.reduced || duration(1) === 0) {
        applyMove(cubies, name);
        solveQueue.shift();
        paint(null);
        render();
        kick();
        return;
      }
      turning = {
        axis: spec.axis,
        layer: spec.layer,
        q: spec.q,
        name,
        forward: true,
        solveStep: true,
        start: performance.now(),
        dur: duration(1),
      };
      ensure();
      return;
    }
    ensureMoves(Math.max(target, display));
    drainBacklog();
    if (display === target) {
      if (rewinding) {
        rewinding = false;
        const typed = typedLength();
        if (!bursting && typed !== display) {
          target = typed;
          ensureMoves(target);
          kick();
          return;
        }
      }
      if (undoAfter) {
        startRewind();
        return;
      }
      if (finale) beginSettle();
      return;
    }
    const forward = target > display;
    const name = forward ? moves[display] : invertMove(moves[display - 1]);
    const spec = parseMove(name);
    const gap = Math.abs(target - display);
    if (opts.reduced || duration(gap) === 0) {
      applyMove(cubies, name);
      display += forward ? 1 : -1;
      paint(null);
      render();
      kick();
      return;
    }
    turning = {
      axis: spec.axis,
      layer: spec.layer,
      q: spec.q,
      name,
      forward,
      start: performance.now(),
      dur: duration(gap),
    };
    ensure();
  }

  function beginSettle() {
    finale = false;
    locked = true;
    if (!opts.reduced) settling = performance.now();
  }

  function readTarget() {
    if (bursting) return;
    finale = false;
    rewinding = false;
    locked = false;
    target = typedLength();
    ensureMoves(target);
    kick();
  }

  function shakeCube() {
    const node = document.getElementById("gate-cube");
    if (!node || opts.reduced) return;
    node.classList.remove("is-shaking");
    void node.offsetWidth;
    node.classList.add("is-shaking");
    node.addEventListener("animationend", () => node.classList.remove("is-shaking"), { once: true });
  }

  function startRewind() {
    undoAfter = false;
    bursting = false;
    finale = false;
    rewinding = true;
    locked = false;
    target = held == null ? typedLength() : held;
    held = null;
    ensure();
    if (!turning) kick();
  }

  function finishTurn() {
    if (!turning) return;
    applyMove(cubies, turning.name);
    if (turning.solveStep) solveQueue.shift();
    else display += turning.forward ? 1 : -1;
    turning = null;
    paint(null);
    if (pendingComplete) beginComplete();
    else kick();
  }

  function pose(now) {
    smooth.x += (look.x - smooth.x) * 0.08;
    smooth.y += (look.y - smooth.y) * 0.08;
    const t = now / 1000;
    let damp = locked ? 0.35 : 1;
    if (settling) {
      const u = Math.min(1, (now - settling) / 320);
      damp = 1 - u;
      if (u >= 1) settling = 0;
    }
    const amp = (opts.reduced ? 0 : 0.055) * damp;
    group.rotation.x = -0.16 + Math.sin(t * 0.85) * amp * 0.55 + smooth.y * -0.14;
    group.rotation.y = 0.34 + Math.sin(t * 0.55) * amp + smooth.x * 0.18;
  }

  function render() {
    renderer.render(scene, camera);
  }

  function frame(now) {
    if (dead || !running) return;
    const gate = document.getElementById("gate");
    if (document.hidden || !gate || gate.hidden) {
      running = false;
      raf = 0;
      return;
    }
    if (turning) {
      const u = Math.min(1, (now - turning.start) / turning.dur);
      if (u >= 1) finishTurn();
      else paint({ axis: turning.axis, layer: turning.layer, angle: turning.q * (Math.PI / 2) * ease(u) });
    }
    pose(now);
    render();
    raf = requestAnimationFrame(frame);
  }

  function ensure() {
    if (dead || running || opts.reduced) return;
    const gate = document.getElementById("gate");
    if (!gate || gate.hidden || document.hidden) return;
    running = true;
    raf = requestAnimationFrame(frame);
  }

  function onInput() {
    readTarget();
  }

  function onPointer(event) {
    if (opts.reduced) return;
    const rect = event.currentTarget.getBoundingClientRect();
    look.x = (event.clientX - rect.left) / rect.width - 0.5;
    look.y = (event.clientY - rect.top) / rect.height - 0.5;
  }

  function onLeave() {
    look.x = 0;
    look.y = 0;
  }

  function onVisible() {
    if (!document.hidden) ensure();
  }

  if (opts.user) opts.user.addEventListener("input", onInput);
  if (opts.pass) opts.pass.addEventListener("input", onInput);
  if (opts.card && !opts.reduced) {
    opts.card.addEventListener("pointermove", onPointer);
    opts.card.addEventListener("pointerleave", onLeave);
  }
  document.addEventListener("visibilitychange", onVisible);

  paint(null);
  render();
  ensure();

  return {
    solve() {
      if (!bursting) held = typedLength();
      bursting = true;
      undoAfter = false;
      rewinding = false;
      finale = true;
      const base = held == null ? typedLength() : held;
      target = base + BURST;
      ensureMoves(target);
      if (display === target && !turning) beginSettle();
      else kick();
    },
    reject() {
      if (solving || pendingComplete) return;
      shakeCube();
      if (display === target && !turning) {
        startRewind();
        return;
      }
      undoAfter = true;
      if (opts.reduced) startRewind();
    },
    complete() {
      if (onSolved) return new Promise((resolve) => {
        const previous = onSolved;
        onSolved = () => {
          previous();
          resolve();
        };
      });
      return new Promise((resolve) => {
        onSolved = resolve;
        bursting = true;
        undoAfter = false;
        if (turning) pendingComplete = true;
        else beginComplete();
      });
    },
    sync() {
      readTarget();
      ensure();
      if (opts.reduced) render();
    },
    pause() {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    },
    dispose() {
      dead = true;
      running = false;
      if (raf) cancelAnimationFrame(raf);
      if (opts.user) opts.user.removeEventListener("input", onInput);
      if (opts.pass) opts.pass.removeEventListener("input", onInput);
      if (opts.card) {
        opts.card.removeEventListener("pointermove", onPointer);
        opts.card.removeEventListener("pointerleave", onLeave);
      }
      document.removeEventListener("visibilitychange", onVisible);
      coreGeo.dispose();
      stickerGeo.dispose();
      for (const material of Object.values(materials)) material.dispose();
      renderer.dispose();
      if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
    },
  };
}
