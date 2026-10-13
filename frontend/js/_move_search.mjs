import { createSolvedCubies, applyMove, isHome } from "./cube.js";

const FACES = ["U", "D", "R", "L", "F", "B"];

function invert(name) {
  return name.endsWith("'") ? name[0] : `${name[0]}'`;
}

function solidCount(cubies) {
  const name = (v) => (v[1] === 1 ? "U" : v[1] === -1 ? "D" : v[0] === 1 ? "R" : v[0] === -1 ? "L" : v[2] === 1 ? "F" : "B");
  const faces = { U: [], D: [], F: [], B: [], R: [], L: [] };
  const homes = [
    ["R", [1, 0, 0], "hx", 1],
    ["L", [-1, 0, 0], "hx", -1],
    ["U", [0, 1, 0], "hy", 1],
    ["D", [0, -1, 0], "hy", -1],
    ["F", [0, 0, 1], "hz", 1],
    ["B", [0, 0, -1], "hz", -1],
  ];
  for (const c of cubies) {
    for (const [color, normal, key, sign] of homes) {
      if (c[key] !== sign) continue;
      const w = [
        Math.round(c.m[0][0] * normal[0] + c.m[0][1] * normal[1] + c.m[0][2] * normal[2]),
        Math.round(c.m[1][0] * normal[0] + c.m[1][1] * normal[1] + c.m[1][2] * normal[2]),
        Math.round(c.m[2][0] * normal[0] + c.m[2][1] * normal[1] + c.m[2][2] * normal[2]),
      ];
      faces[name(w)].push(color);
    }
  }
  let solid = 0;
  for (const cols of Object.values(faces)) if (new Set(cols).size === 1) solid += 1;
  return solid;
}

function clone(cubies) {
  return cubies.map((c) => ({ ...c, m: c.m.map((row) => row.slice()) }));
}

function pick(cube, moves, i, maxSolid) {
  for (let k = 0; k < 36; k += 1) {
    const n = i * 11 + k * 5 + 3;
    const face = FACES[n % 6];
    const name = (n & 4) ? `${face}'` : face;
    const prev = moves[moves.length - 1];
    if (prev && name[0] === prev[0]) continue;
    const trial = clone(cube);
    applyMove(trial, name);
    if (isHome(trial)) continue;
    if (solidCount(trial) > maxSolid) continue;
    return { name, cube: trial };
  }
  return null;
}

const moves = [];
let cube = createSolvedCubies();
for (let i = 0; i < 200; i += 1) {
  const maxSolid = i < 5 ? 5 : 0;
  const found = pick(cube, moves, i, maxSolid);
  if (!found) {
    console.log("stuck", i, "solid", solidCount(cube), moves.slice(-8).join(" "));
    break;
  }
  moves.push(found.name);
  cube = found.cube;
}
console.log(JSON.stringify(moves));
console.log("len", moves.length, "home", isHome(cube));
for (let i = moves.length - 1; i >= 0; i -= 1) applyMove(cube, invert(moves[i]));
console.log("back", isHome(cube));
