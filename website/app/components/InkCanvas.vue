<template>
  <div ref="host" class="relative h-full w-full overflow-hidden" :style="{ background: paper }">
    <canvas ref="canvas" class="block h-full w-full" :aria-label="alt" role="img"></canvas>
  </div>
</template>

<script setup lang="ts">
import { useElementVisibility, usePreferredReducedMotion } from '@vueuse/core'

// Renders traced ink drawings shape by shape. The drawing stays intact; the motions live around it:
// converge: small rays drift a little toward the anchors · lanes: small ticks slide along their lane
// flow: small shapes drift along the line while the red mark pulses · draw: the drawing sweeps in from the left
// align: shapes left of the red bar shiver. Every mode: the cursor gently parts the ink.
//
// Several drawings can share one canvas: `frames` stack in order and each is revealed from the left by its
// `reveals` entry (0–1), which is how the landing page wipes between beats without extra layers.
//
// Rendering: every shape is filled once into a sprite atlas (cutouts baked into their parent), the atlas
// goes to the GPU as a texture array, and each visible drawing is one instanced draw call of textured quads.
// The CPU only updates four floats per shape. Without WebGL2 the sprites are blitted once, static.
export type InkMotion = 'still' | 'converge' | 'lanes' | 'flow' | 'draw' | 'align'
export interface InkFrame { src: string; motion?: InkMotion }

// p: index of the ink shape this paper-coloured cutout belongs to (-1 for ink) · a: tone · r: vermilion · k: anchor
interface StrokeData { d: string; x: number; y: number; w: number; h: number; r: boolean; a: number; p: number; k: boolean }
interface InkData { w: number; h: number; strokes: StrokeData[] }

const props = withDefaults(defineProps<{
  /** a single drawing … */
  src?: string
  motion?: InkMotion
  /** … or several, stacked, each revealed from the left by `reveals[i]` */
  frames?: InkFrame[]
  reveals?: number[]
  alt?: string
  paper?: string
  ink?: string
  accent?: string
  /** whether anything on this canvas is worth animating right now */
  active?: boolean
  /** load at once (above the fold); otherwise wait until the canvas nears the viewport */
  eager?: boolean
}>(), { motion: 'still', alt: '', paper: '#f7f4ee', ink: '#17150f', accent: '#e8471f', active: true, eager: false })

const host = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const visible = useElementVisibility(host)
const near = useElementVisibility(host, { rootMargin: '600px 0px' })
const reduced = usePreferredReducedMotion()

const fract = (v: number) => v - Math.floor(v)
const smooth = (t: number) => t * t * (3 - 2 * t)
const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t)
// cheap deterministic noise per (step, index) so a shiver is jittery but not random every frame
const hash = (a: number, b: number) => fract(Math.sin(a * 12.9898 + b * 78.233) * 43758.5453)
const rgb = (hex: string) => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255) as [number, number, number]
const idle = () => new Promise<void>(r => ('requestIdleCallback' in window ? requestIdleCallback(() => r(), { timeout: 2500 }) : setTimeout(r, 800)))

// one fetch and parse per drawing, however many canvases show it
const cache = new Map<string, Promise<InkData>>()
const load = (src: string) => cache.get(src) ?? cache.set(src, $fetch<InkData>(src)).get(src)!

const ATLAS_W = 2048
const ATLAS_H = 4096
const PAD = 2

const VERT = `#version 300 es
layout(location=0) in vec2 corner;
layout(location=1) in vec4 rect;   // sx, sy, sw, sh in atlas pixels
layout(location=2) in vec3 place;  // sprite offset from the shape centre (px), atlas layer
layout(location=3) in vec4 dyn;    // centre x, centre y (px), rotation, alpha
uniform vec2 uView;
uniform vec2 uAtlas;
out vec2 vUv;
out float vLayer;
out float vAlpha;
void main() {
  vec2 local = place.xy + corner * rect.zw;
  float c = cos(dyn.z), s = sin(dyn.z);
  vec2 p = dyn.xy + vec2(local.x * c - local.y * s, local.x * s + local.y * c);
  vec2 clip = (p / uView) * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  vUv = (rect.xy + corner * rect.zw) / uAtlas;
  vLayer = place.z;
  vAlpha = dyn.w;
}`

const FRAG = `#version 300 es
precision mediump float;
uniform mediump sampler2DArray uTex;
in vec2 vUv;
in float vLayer;
in float vAlpha;
out vec4 outColor;
void main() {
  outColor = texture(uTex, vec3(vUv, vLayer)) * vAlpha;
}`

// Everything one drawing needs on the GPU and for its motion.
interface Frame {
  motion: InkMotion
  data: InkData
  paths: Path2D[]
  inkIdx: number[]
  children: number[][]
  bounds: Float32Array
  phase: Float32Array
  ox: Float32Array
  oy: Float32Array
  isTick: Uint8Array
  isSmall: Uint8Array
  ax: number
  ay: number
  gateX: number
  redX: number
  rectBuf: Float32Array
  placeBuf: Float32Array
  dynBuf: Float32Array
  atlases: HTMLCanvasElement[]
  atlasH: number
  tex: WebGLTexture | null
  rectVbo: WebGLBuffer | null
  placeVbo: WebGLBuffer | null
  dynVbo: WebGLBuffer | null
  vao: WebGLVertexArrayObject | null
}

function prepare(data: InkData, motion: InkMotion): Frame {
  const { w: W, h: H } = data
  const N = data.strokes.length
  const inkIdx: number[] = []
  const children: number[][] = Array.from({ length: N }, () => [])
  data.strokes.forEach((s, i) => (s.p === -1 ? inkIdx.push(i) : children[s.p]!.push(i)))
  const M = inkIdx.length

  // sprite bounds in drawing units, relative to the shape centre, including its cutouts
  const bounds = new Float32Array(N * 4)
  for (const i of inkIdx) {
    const s = data.strokes[i]!
    let x0 = -s.w / 2, y0 = -s.h / 2, x1 = s.w / 2, y1 = s.h / 2
    for (const c of children[i]!) {
      const k = data.strokes[c]!
      x0 = Math.min(x0, k.x - s.x - k.w / 2)
      y0 = Math.min(y0, k.y - s.y - k.h / 2)
      x1 = Math.max(x1, k.x - s.x + k.w / 2)
      y1 = Math.max(y1, k.y - s.y + k.h / 2)
    }
    bounds.set([x0, y0, x1, y1], i * 4)
  }
  const phase = new Float32Array(N)
  for (let i = 0; i < N; i++) phase[i] = Math.random()

  // landmarks each motion steers by
  const ink = inkIdx.map(i => data.strokes[i]!)
  const mean = (list: StrokeData[], key: 'x' | 'y', fallback: number) =>
    list.length ? list.reduce((a, s) => a + s[key], 0) / list.length : fallback
  const isTick = new Uint8Array(N)
  const isSmall = new Uint8Array(N)
  for (const i of inkIdx) {
    const s = data.strokes[i]!
    isSmall[i] = s.w < W * 0.06 && s.h < H * 0.12 ? 1 : 0
    // a lane tick is a short vertical sliver; rail fragments are wide and must stay put
    isTick[i] = s.h > s.w * 1.2 && s.w < W * 0.02 && s.h < H * 0.1 ? 1 : 0
  }
  return {
    motion, data, inkIdx, children, bounds, phase, isTick, isSmall,
    paths: data.strokes.map(s => new Path2D(s.d)),
    ox: new Float32Array(N), oy: new Float32Array(N),
    ax: mean(ink.filter(s => s.k), 'x', W / 2), ay: mean(ink.filter(s => s.k), 'y', H / 2),
    gateX: mean(ink.filter(s => s.h > H * 0.4), 'x', W * 0.45), redX: mean(ink.filter(s => s.r), 'x', W * 0.3),
    rectBuf: new Float32Array(M * 4), placeBuf: new Float32Array(M * 3), dynBuf: new Float32Array(M * 4),
    atlases: [], atlasH: ATLAS_H, tex: null, rectVbo: null, placeVbo: null, dynVbo: null, vao: null,
  }
}

onMounted(async () => {
  const el = canvas.value
  const wrap = host.value
  if (!el || !wrap) return

  const specs: InkFrame[] = props.frames ?? (props.src ? [{ src: props.src, motion: props.motion }] : [])
  if (!specs.length) return
  const revealOf = (i: number) => (props.reveals ? clamp01(props.reveals[i] ?? 0) : 1)

  // above the fold the first drawing loads right away; below it, nothing is fetched until the reader gets close
  if (!props.eager) await new Promise<void>((r) => { if (near.value) r(); else { const stop = watch(near, (v) => { if (v) { stop(); r() } }) } })
  if (!props.active) await idle()
  const frames: (Frame | null)[] = specs.map(() => null)
  frames[0] = prepare(await load(specs[0]!.src), specs[0]!.motion ?? 'still')

  // ---- GPU setup ---------------------------------------------------------------------------------
  const gl = el.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false, premultipliedAlpha: true, powerPreference: 'high-performance' })
  const paperRgb = rgb(props.paper)
  let program: WebGLProgram | null = null
  let uView: WebGLUniformLocation | null = null
  let uAtlas: WebGLUniformLocation | null = null
  let quadVbo: WebGLBuffer | null = null
  let ctx2d: CanvasRenderingContext2D | null = null

  function compile(type: number, src: string) {
    const sh = gl!.createShader(type)!
    gl!.shaderSource(sh, src)
    gl!.compileShader(sh)
    if (!gl!.getShaderParameter(sh, gl!.COMPILE_STATUS)) throw new Error(gl!.getShaderInfoLog(sh) ?? 'shader')
    return sh
  }

  function initGl() {
    if (!gl) return false
    try {
      program = gl.createProgram()!
      gl.attachShader(program, compile(gl.VERTEX_SHADER, VERT))
      gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAG))
      gl.linkProgram(program)
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) ?? 'link')
    }
    catch {
      return false
    }
    gl.useProgram(program)
    uView = gl.getUniformLocation(program, 'uView')
    uAtlas = gl.getUniformLocation(program, 'uAtlas')
    quadVbo = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, quadVbo)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1]), gl.STATIC_DRAW)
    gl.enable(gl.BLEND)
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA)
    gl.clearColor(paperRgb[0], paperRgb[1], paperRgb[2], 1)
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true)
    return true
  }

  // one vertex array per drawing: the unit quad plus its three instanced attribute buffers
  function attach(f: Frame) {
    if (!gl) return
    f.vao = gl.createVertexArray()
    gl.bindVertexArray(f.vao)
    gl.bindBuffer(gl.ARRAY_BUFFER, quadVbo)
    gl.enableVertexAttribArray(0)
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0)
    const inst = (loc: number, size: number, data: Float32Array, usage: number) => {
      const vbo = gl.createBuffer()
      gl.bindBuffer(gl.ARRAY_BUFFER, vbo)
      gl.bufferData(gl.ARRAY_BUFFER, data, usage)
      gl.enableVertexAttribArray(loc)
      gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0)
      gl.vertexAttribDivisor(loc, 1)
      return vbo
    }
    f.rectVbo = inst(1, 4, f.rectBuf, gl.STATIC_DRAW)
    f.placeVbo = inst(2, 3, f.placeBuf, gl.STATIC_DRAW)
    f.dynVbo = inst(3, 4, f.dynBuf, gl.DYNAMIC_DRAW)
    gl.bindVertexArray(null)
    f.tex = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D_ARRAY, f.tex)
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
  }

  let scale = 1
  let tx = 0
  let ty = 0
  let dpr = 1
  let ss = 1 // drawing units → device pixels

  function bake(f: Frame) {
    const { inkIdx, bounds, rectBuf, placeBuf, data, paths, children } = f
    const M = inkIdx.length
    f.atlases = []
    // pass 1: shelf-pack tall sprites first, so we know how much atlas each layer really needs
    const order = [...Array(M).keys()].sort((a, b) => {
      const ia = inkIdx[a]!, ib = inkIdx[b]!
      return (bounds[ib * 4 + 3]! - bounds[ib * 4 + 1]!) - (bounds[ia * 4 + 3]! - bounds[ia * 4 + 1]!)
    })
    let layer = -1
    let cx = 0
    let cy = 0
    let rowH = 0
    let used = 0
    for (const j of order) {
      const i = inkIdx[j]!
      const x0 = bounds[i * 4]!, y0 = bounds[i * 4 + 1]!, x1 = bounds[i * 4 + 2]!, y1 = bounds[i * 4 + 3]!
      const sw = Math.min(ATLAS_W, Math.ceil((x1 - x0) * ss) + PAD * 2)
      const sh = Math.min(ATLAS_H, Math.ceil((y1 - y0) * ss) + PAD * 2)
      if (layer < 0 || cx + sw > ATLAS_W) {
        cx = 0
        cy += rowH
        rowH = 0
      }
      if (layer < 0 || cy + sh > ATLAS_H) {
        layer++
        cx = 0
        cy = 0
        rowH = 0
      }
      rectBuf.set([cx, cy, sw, sh], j * 4)
      placeBuf.set([x0 * ss - PAD, y0 * ss - PAD, layer], j * 3)
      cx += sw
      rowH = Math.max(rowH, sh)
      used = Math.max(used, cy + rowH)
    }
    // every layer shares one height; a single small layer stays small
    f.atlasH = layer === 0 ? Math.min(ATLAS_H, Math.ceil(used / 64) * 64) : ATLAS_H
    for (let l = 0; l <= layer; l++) {
      const c = document.createElement('canvas')
      c.width = ATLAS_W
      c.height = f.atlasH
      f.atlases.push(c)
    }
    // pass 2: fill the sprites, cutouts baked over their parent in paper colour
    for (let j = 0; j < M; j++) {
      const i = inkIdx[j]!
      const s = data.strokes[i]!
      const ac = f.atlases[placeBuf[j * 3 + 2]!]!.getContext('2d')!
      const originX = rectBuf[j * 4]! + PAD - bounds[i * 4]! * ss
      const originY = rectBuf[j * 4 + 1]! + PAD - bounds[i * 4 + 1]! * ss
      ac.setTransform(ss, 0, 0, ss, originX, originY)
      ac.globalAlpha = s.a
      ac.fillStyle = s.r ? props.accent : props.ink
      ac.fill(paths[i]!)
      ac.globalAlpha = 1
      ac.fillStyle = props.paper
      for (const c of children[i]!) {
        const k = data.strokes[c]!
        ac.setTransform(ss, 0, 0, ss, originX + (k.x - s.x) * ss, originY + (k.y - s.y) * ss)
        ac.fill(paths[c]!)
      }
    }
    if (!gl || !program) return
    if (!f.vao) attach(f)
    gl.bindTexture(gl.TEXTURE_2D_ARRAY, f.tex)
    gl.texImage3D(gl.TEXTURE_2D_ARRAY, 0, gl.RGBA, ATLAS_W, f.atlasH, f.atlases.length, 0, gl.RGBA, gl.UNSIGNED_BYTE, null)
    f.atlases.forEach((c, l) => gl.texSubImage3D(gl.TEXTURE_2D_ARRAY, 0, 0, 0, l, ATLAS_W, f.atlasH, 1, gl.RGBA, gl.UNSIGNED_BYTE, c))
    gl.bindBuffer(gl.ARRAY_BUFFER, f.rectVbo)
    gl.bufferData(gl.ARRAY_BUFFER, rectBuf, gl.STATIC_DRAW)
    gl.bindBuffer(gl.ARRAY_BUFFER, f.placeVbo)
    gl.bufferData(gl.ARRAY_BUFFER, placeBuf, gl.STATIC_DRAW)
  }

  const useGl = initGl()
  if (!useGl) ctx2d = el.getContext('2d')

  const pointer = { x: -1e9, y: -1e9 }
  const onMove = (e: PointerEvent) => {
    const r = wrap.getBoundingClientRect()
    pointer.x = (e.clientX - r.left - tx) / scale
    pointer.y = (e.clientY - r.top - ty) / scale
  }
  const onLeave = () => { pointer.x = -1e9; pointer.y = -1e9 }

  // where every ink shape of a drawing sits this frame → its dynBuf
  function simulate(f: Frame, t: number) {
    const { data, inkIdx, phase, ox, oy, isTick, isSmall, ax, ay, gateX, redX, dynBuf } = f
    const { w: W } = data
    const R = W * 0.06
    const step = Math.floor(t * 8)
    const motion = reduced.value === 'reduce' ? 'still' : f.motion
    const offX = tx * dpr
    const offY = ty * dpr
    for (let j = 0; j < inkIdx.length; j++) {
      const i = inkIdx[j]!
      const s = data.strokes[i]!
      let x = 0
      let y = 0
      let a = 1
      let r = 0

      if (motion === 'converge' && !s.k && isSmall[i]) {
        const vx = ax - s.x
        const vy = ay - s.y
        const dist = Math.hypot(vx, vy) || 1
        const k = fract(phase[i]! + t * 0.06)
        const travel = Math.min(dist * 0.12, W * 0.03)
        x = (vx / dist) * travel * k
        y = (vy / dist) * travel * k
        a = Math.min(1, k * 10, (1 - k) * 10)
      }
      else if (motion === 'lanes' && s.x > gateX && isTick[i] && !s.r) {
        // ticks slide a short way along their lane and wrap back, so the lanes look like they run
        const k = fract(phase[i]! + t * 0.1)
        x = (k - 0.5) * W * 0.025
        a = Math.min(1, k * 12, (1 - k) * 12)
      }
      else if (motion === 'flow') {
        if (s.r) {
          a = 0.75 + 0.25 * Math.sin(t * 2.2)
        }
        else if (isTick[i]) {
          const k = fract(phase[i]! + t * 0.08)
          x = (k - 0.5) * W * 0.02
          a = Math.min(1, k * 10, (1 - k) * 10)
        }
      }
      else if (motion === 'align' && !s.r) {
        const chaos = clamp01((redX - s.x) / (W * 0.25))
        if (chaos) {
          const h = hash(step, i)
          x = (h - 0.5) * 7 * chaos
          y = (fract(h * 7.31) - 0.5) * 7 * chaos
          r = 0.03 * chaos * (h - 0.5)
        }
      }

      // the cursor parts the ink
      const px = s.x + x - pointer.x
      const py = s.y + y - pointer.y
      const d2 = px * px + py * py
      let txx = 0
      let tyy = 0
      if (d2 < R * R * 4) {
        const d = Math.sqrt(d2) + 1
        const g = Math.exp(-d2 / (2 * R * R)) * R * 0.5
        txx = (px / d) * g
        tyy = (py / d) * g
      }
      ox[i] = ox[i]! + (txx - ox[i]!) * 0.12
      oy[i] = oy[i]! + (tyy - oy[i]!) * 0.12

      dynBuf[j * 4] = (s.x + x + ox[i]!) * ss + offX
      dynBuf[j * 4 + 1] = (s.y + y + oy[i]!) * ss + offY
      dynBuf[j * 4 + 2] = r
      dynBuf[j * 4 + 3] = a
    }
  }

  let lastT = 0.35
  function draw(t: number) {
    lastT = t
    // the frames worth drawing: revealed at all, and not fully hidden under a later one
    let first = 0
    for (let i = frames.length - 1; i > 0; i--) {
      if (frames[i] && revealOf(i) >= 1) { first = i; break }
    }
    if (gl && program) {
      gl.clear(gl.COLOR_BUFFER_BIT)
      gl.uniform2f(uView, el!.width, el!.height)
      for (let i = first; i < frames.length; i++) {
        const f = frames[i]
        const r = revealOf(i)
        if (!f || !f.atlases.length || r <= 0) continue
        simulate(f, t)
        let edge = el!.width * r
        if (f.motion === 'draw' && reduced.value !== 'reduce') {
          // 9s loop: 6s of drawing, a hold, then the page is wiped for the next pass
          const p = fract(t / 9)
          edge = Math.min(edge, f.data.w * 1.04 * smooth(clamp01(p / 0.66)) * ss + tx * dpr)
        }
        if (r < 1 || edge < el!.width) {
          // the revealed strip replaces what was under it: back to paper, then this drawing
          gl.enable(gl.SCISSOR_TEST)
          gl.scissor(0, 0, Math.max(0, Math.round(edge)), el!.height)
          gl.clear(gl.COLOR_BUFFER_BIT)
        }
        gl.bindVertexArray(f.vao)
        gl.bindTexture(gl.TEXTURE_2D_ARRAY, f.tex)
        gl.uniform2f(uAtlas, ATLAS_W, f.atlasH)
        gl.bindBuffer(gl.ARRAY_BUFFER, f.dynVbo)
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, f.dynBuf)
        gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, f.inkIdx.length)
        gl.disable(gl.SCISSOR_TEST)
      }
      gl.bindVertexArray(null)
    }
    else if (ctx2d) {
      // fallback: one static blit of every sprite of the topmost visible drawing
      const f = frames[first]
      if (!f || !f.atlases.length) return
      simulate(f, 0.35)
      ctx2d.setTransform(1, 0, 0, 1, 0, 0)
      ctx2d.clearRect(0, 0, el!.width, el!.height)
      const { rectBuf, placeBuf, dynBuf } = f
      for (let j = 0; j < f.inkIdx.length; j++) {
        ctx2d.drawImage(f.atlases[placeBuf[j * 3 + 2]!]!, rectBuf[j * 4]!, rectBuf[j * 4 + 1]!, rectBuf[j * 4 + 2]!, rectBuf[j * 4 + 3]!,
          dynBuf[j * 4]! + placeBuf[j * 3]!, dynBuf[j * 4 + 1]! + placeBuf[j * 3 + 1]!, rectBuf[j * 4 + 2]!, rectBuf[j * 4 + 3]!)
      }
    }
  }

  let bakeTimer = 0
  const rebakeAll = () => { for (const f of frames) if (f) bake(f); draw(lastT) }
  const resize = () => {
    dpr = Math.min(window.devicePixelRatio || 1, 2)
    const cw = wrap.clientWidth
    const ch = wrap.clientHeight
    if (!cw || !ch) return
    el.width = Math.round(cw * dpr)
    el.height = Math.round(ch * dpr)
    // every drawing in the stack has the same size; cover the box, like object-fit: cover
    const { w: W, h: H } = frames[0]!.data
    scale = Math.max(cw / W, ch / H)
    tx = (cw - W * scale) / 2
    ty = (ch - H * scale) / 2
    ss = scale * dpr
    gl?.viewport(0, 0, el.width, el.height)
    clearTimeout(bakeTimer)
    // first layout bakes at once; later resizes wait until they settle
    if (!frames[0]!.atlases.length) rebakeAll()
    else bakeTimer = window.setTimeout(rebakeAll, 150)
    draw(lastT)
  }

  const ro = new ResizeObserver(resize)
  ro.observe(wrap)
  resize()

  // the other drawings load one step ahead of the reader: frame i starts once frame i-1 is the one on show
  const loading = new Set<number>()
  const ensure = async (i: number) => {
    if (i >= specs.length || frames[i] || loading.has(i)) return
    loading.add(i)
    const f = prepare(await load(specs[i]!.src), specs[i]!.motion ?? 'still')
    if (ss !== 1) bake(f)
    frames[i] = f
  }
  idle().then(() => ensure(1))
  watch(() => props.reveals?.map(r => r > 0), (shown) => {
    shown?.forEach((on, i) => { if (on) ensure(i + 1) })
  }, { immediate: true })

  let frame = 0
  let start = performance.now()
  const tick = (now: number) => {
    frame = requestAnimationFrame(tick)
    if (lost || !visible.value || !props.active) return
    const t0 = performance.now()
    draw((now - start) / 1000)
    if (import.meta.dev) (window as any).__inkMs = performance.now() - t0
  }

  if (reduced.value !== 'reduce' && useGl) {
    wrap.addEventListener('pointermove', onMove, { passive: true })
    wrap.addEventListener('pointerleave', onLeave)
    start = performance.now()
    frame = requestAnimationFrame(tick)
  }
  else {
    watch(() => props.reveals, () => draw(lastT), { deep: true })
  }

  // a GPU reset or a backgrounded tab can drop the context: stop drawing, then rebuild everything on restore
  let lost = false
  const onLost = (e: Event) => { e.preventDefault(); lost = true }
  const onRestored = () => {
    for (const f of frames) if (f) { f.vao = null; f.tex = null; f.rectVbo = null; f.placeVbo = null; f.dynVbo = null }
    if (initGl()) { gl!.viewport(0, 0, el.width, el.height); rebakeAll() }
    lost = false
  }
  el.addEventListener('webglcontextlost', onLost)
  el.addEventListener('webglcontextrestored', onRestored)

  onUnmounted(() => {
    cancelAnimationFrame(frame)
    clearTimeout(bakeTimer)
    ro.disconnect()
    wrap.removeEventListener('pointermove', onMove)
    wrap.removeEventListener('pointerleave', onLeave)
    el.removeEventListener('webglcontextlost', onLost)
    el.removeEventListener('webglcontextrestored', onRestored)
    if (gl) {
      for (const f of frames) if (f) {
        gl.deleteTexture(f.tex)
        gl.deleteBuffer(f.rectVbo)
        gl.deleteBuffer(f.placeVbo)
        gl.deleteBuffer(f.dynVbo)
        gl.deleteVertexArray(f.vao)
      }
      gl.deleteBuffer(quadVbo)
      gl.deleteProgram(program)
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    }
  })
})
</script>
