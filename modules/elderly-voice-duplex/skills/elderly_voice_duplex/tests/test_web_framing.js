#!/usr/bin/env node
/**
 * 网页客户端攒帧逻辑回归测试。
 *
 * 这条用例是冲着踩过的坑写的：createScriptProcessor 的 buffer 大小必须是
 * 2 的幂（256~16384），而客户端原来把"每帧样本数 320"当成了毫秒数去算
 * buffer，得到 5120——浏览器直接抛
 *   "buffer size (5120) must be 0 or a power of two between 256 and 16384"
 * 麦克风根本起不来。
 *
 * 现在 buffer 固定取 2048，再自己攒够 320 样本才发一帧。这里验证：
 *   1. 帧数、每帧样本数、样本总数都不折不扣
 *   2. 样本顺序连续，没有重复或错位
 *   3. 非整数倍能正确续上
 *   4. 浏览器忽略请求的 16kHz 时（Safari 会），48k 输入被重采样
 *   5. 编码产出的是真 ArrayBuffer，且 PCM16 能原样解回
 *
 * 直接读 web/index.html 里的函数来跑，不复制一份逻辑——复制了就验证不到
 * 真正在线上跑的那段代码。特别注意 floatTo16BitPCM 也用真的：早期测试
 * 把它换成假的"原样返回"，正好把 new DataView(长度) 这个真 bug 掩护过去了。
 *
 * 用法： node tests/test_web_framing.js
 */
"use strict";

const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "..", "web", "index.html");
const FRAME_SAMPLES = 320;      // 20ms @ 16kHz，必须和 index.html 一致
const SAMPLE_RATE = 16000;
const PROC_BUFFER = 2048;       // 2 的幂，createScriptProcessor 要求
const VAD_THRESHOLD = 0.35;     // 必须和 config.VAD_ENERGY_THRESHOLD 一致

const html = fs.readFileSync(HTML, "utf8");

/** 从 HTML 里把某个函数体原样抠出来。 */
function extract(name) {
  const start = html.indexOf("function " + name + "(");
  if (start < 0) throw new Error("web/index.html 里找不到 " + name);
  let depth = 0;
  const open = html.indexOf("{", start);
  for (let i = open; i < html.length; i++) {
    if (html[i] === "{") depth++;
    else if (html[i] === "}") {
      depth--;
      if (depth === 0) return html.slice(start, i + 1);
    }
  }
  throw new Error(name + " 的花括号不配对");
}

// 假的 ws：记录每次 send，便于分辨二进制帧和 JSON 控制帧
function makePusher(micRate) {
  const sent = [];
  const ws = { readyState: 1, send(data) { sent.push(data); } };
  // sendMicFrame 现在会顺手更新自检面板，这里把 DOM 和计数器都假掉。
  // 注意：floatTo16BitPCM 用**真的**，不造假实现——早期就是在这里造假
  // 返回值，把 "new DataView(长度)" 这个真 bug 一起掩护过去了。
  const stubs = [
    "const VAD_THRESHOLD = 0.35;",
    "const meterEl = { hidden: true };",
    "const mRate = { textContent: '' }, mFps = { textContent: '' };",
    "const mBar = { style: {} }, mThr = { style: {} };",
    "const mEnergy = { textContent: '' }, mSpoken = { textContent: '' };",
    "const mVerdict = { textContent: '', className: '' };",
    "let framesSent = 0, speechMs = 0, sendError = '';",
    "function updateMeter() {}",
  ].join("\n");
  const src = [
    "let micRemainder = new Float32Array(0);",
    "let micRate = " + micRate + ";",
    extract("frameEnergy"),
    extract("floatTo16BitPCM"),
    extract("resampleTo16k"),
    extract("pushMicFrame"),
    extract("sendMicFrame"),
    "return { pushMicFrame, frameEnergy, floatTo16BitPCM,",
    "         getState: () => ({ framesSent, speechMs, sendError }) };",
  ].join("\n");
  const api = new Function("FRAME_SAMPLES", "SAMPLE_RATE", "connected", "ws",
                           stubs + "\n" + src)(FRAME_SAMPLES, SAMPLE_RATE,
                                              true, ws);
  return { push: api.pushMicFrame,
           frameEnergy: api.frameEnergy,
           floatTo16BitPCM: api.floatTo16BitPCM,
           state: api.getState,
           audioFrames: () => sent.filter(d => typeof d !== "string"),
           ctrlFrames: () => sent.filter(d => typeof d === "string") };
}

function check(name, cond, detail) {
  console.log((cond ? "  PASS  " : "  FAIL  ") + name +
              (detail && !cond ? "  -- " + detail : ""));
  return cond;
}

/** 一帧 PCM16 能否解回成原来的 float 样本（误差在 1 个 LSB 内）。 */
function decodedOk(blob) {
  const dv = new DataView(blob);
  const n = blob.byteLength / 2;
  for (let i = 0; i < n; i++) {
    const back = dv.getInt16(i * 2, true) / 32768;
    if (Math.abs(back) > 1) return false;
  }
  return true;
}

function main() {
  let ok = true;
  console.log("[web-framing] 攒帧逻辑");

  const total = PROC_BUFFER * 5;         // 10240 样本
  const main16 = makePusher(SAMPLE_RATE);
  const { push, audioFrames, ctrlFrames, state } = main16;

  // 喂 -1..1 的斜坡。样本必须落在 [-1,1]：PCM 编码会 clamp，超范围的值
  // 全变成 32767，就验不出顺序了。
  let seq = 0;
  for (let k = 0; k < 5; k++) {
    const buf = new Float32Array(PROC_BUFFER);
    for (let i = 0; i < PROC_BUFFER; i++) {
      buf[i] = -1 + 2 * (seq++ / total);
    }
    push(buf);
  }
  // sendMicFrame 自己会吞掉发送异常（为了让自检面板显示"发送失败"），
  // 所以这里不能靠抛异常发现，必须查 sendError 和实际发出的帧数。
  // 少了这一条，编码里写的 bug 就会表现成"面板说已收音、服务端零帧"。
  ok &= check("没有发送错误", state().sendError === "",
              state().sendError);
  ok &= check("真的发出了帧", audioFrames().length > 0,
              "发出 " + audioFrames().length + " 帧");
  if (audioFrames().length === 0) {
    console.log("[web-framing] FAIL");
    return 1;
  }

  // 送出去的是编码后的 PCM16，样本内容要从字节解回来看——这正好把
  // 「切帧 -> 编码 -> 解码」整条路都验了。必须现算，用快照会比不出
  // 后面新增的帧。
  const blobs = () => audioFrames();
  const decoded = () => blobs().map(b => {
    const dv = new DataView(b);
    const out = new Float64Array(b.byteLength / 2);
    for (let i = 0; i < out.length; i++) out[i] = dv.getInt16(i * 2, true);
    return out;
  });
  const flat = () => {
    const out = [];
    decoded().forEach(f => out.push.apply(out, Array.from(f)));
    return out;
  };
  const f0 = flat();
  let monotonic = true;
  for (let i = 1; i < f0.length; i++) {
    if (f0[i] < f0[i - 1] - 1) { monotonic = false; break; }
  }

  // 编码结果必须是真 ArrayBuffer、长度正好 2×样本数、内容能原样解回来。
  // 这条是冲着踩过的坑写的：floatTo16BitPCM 里写过 new DataView(长度)，
  // 抛 "First argument to Dataview constructor must be an ArrayBuffer"，
  // 于是每帧都在发送前炸掉——自检面板因为 framesSent 已自增照样显示
  // "已收音"，服务端却一帧收不到。
  const allArrayBuffer = blobs().every(b => b instanceof ArrayBuffer);
  const sizesOk = blobs().every(b => b.byteLength === FRAME_SAMPLES * 2);
  console.log("  编码后每帧 " + blobs()[0].byteLength + " 字节");
  ok &= check("编码产出的是 ArrayBuffer（不是 Float32Array）", allArrayBuffer,
              typeof blobs()[0]);
  ok &= check("每帧字节数 = 样本数 × 2", sizesOk,
              blobs().map(b => b.byteLength).join(","));
  ok &= check("PCM16 小端编码可原样解回", decodedOk(blobs()[0]));

  // 每帧必须配一条 JSON 控制帧，且 type/energy 都在
  const ctrls = ctrlFrames().map(s => JSON.parse(s));
  ok &= check("每帧都紧跟一条 audio 控制帧",
              ctrls.length === blobs().length,
              "音频 " + blobs().length + " 帧 / 控制 " + ctrls.length + " 条");
  ok &= check("控制帧带 type=audio 和 energy",
              ctrls.every(c => c.type === "audio"
                               && typeof c.energy === "number"
                               && c.energy >= 0 && c.energy <= 1));

  console.log("  喂入 " + total + " 样本，收到 " + blobs().length + " 帧");
  ok &= check("帧数正确", blobs().length === total / FRAME_SAMPLES,
              "实际 " + blobs().length + "，期望 " + total / FRAME_SAMPLES);
  ok &= check("每帧都是 " + FRAME_SAMPLES + " 样本",
              blobs().every(b => b.byteLength === FRAME_SAMPLES * 2),
              "出现 " + [...new Set(blobs().map(b => b.byteLength))]);
  ok &= check("样本总数一致（无丢失）", flat().length === total,
              "实际 " + flat().length + "，期望 " + total);
  ok &= check("样本顺序连续（无重复/错位）",
              monotonic && f0[0] >= -32768 && f0[f0.length - 1] <= 32767,
              "首样本 " + f0[0] + "，末样本 " + f0[f0.length - 1]);

  // 非整数倍要能续上
  push(new Float32Array(100));
  ok &= check("再喂 100 样本后帧数不变",
              blobs().length === total / FRAME_SAMPLES);
  push(new Float32Array(220));
  ok &= check("补足 320 后正好多一帧",
              blobs().length === total / FRAME_SAMPLES + 1,
              "实际 " + blobs().length + "，期望 "
              + (total / FRAME_SAMPLES + 1));

  // Safari 会忽略 AudioContext 请求的 sampleRate，按硬件速率（常见 48k）
  // 采样本。这时必须重采样，否则服务端按 16kHz 解释，老人听到快三倍的声音。
  const hi = makePusher(48000);
  hi.push(new Float32Array(PROC_BUFFER));
  const resampled = Math.floor(PROC_BUFFER * SAMPLE_RATE / 48000);
  const expect = Math.floor(resampled / FRAME_SAMPLES);
  ok &= check("48kHz 输入被重采样到 16kHz 后再攒帧",
              hi.audioFrames().length === expect && expect > 0,
              "实际 " + hi.audioFrames().length + " 帧，期望 " + expect);

  // 能量口径必须和服务端 config.VAD_ENERGY_THRESHOLD 一致：
  // int16 幅值的 RMS 除 3000。这里用一个恒定幅值的帧，能直接手算出期望值。
  // 客户端曾经用 float32 的 RMS × 4，正常说话只有 0.1~0.3，掉在阈值下面，
  // 整句被当静音丢掉——症状就是"接听了说话没反应"。
  const AMP = 6000;                       // int16 恒定幅值
  const frame = new Float32Array(FRAME_SAMPLES).fill(AMP / 32768);
  const want = AMP / 3000;                // = 2.0，会被 clamp 到 1
  const got = main16.frameEnergy(frame);
  console.log("  幅值 " + AMP + " 的帧，客户端能量 " + got.toFixed(3));
  ok &= check("能量按 int16 RMS/3000 计算（与服务端口径一致）",
              Math.abs(got - Math.min(1, want)) < 1e-6,
              "实际 " + got + "，期望 " + Math.min(1, want));
  ok &= check("该幅值越过 VAD 阈值", got >= VAD_THRESHOLD,
              "能量 " + got + " 低于阈值 " + VAD_THRESHOLD);
  // 安静一点的正常说话也要能过线：幅值 1500 -> 0.5
  const quiet = main16.frameEnergy(
    new Float32Array(FRAME_SAMPLES).fill(1500 / 32768));
  ok &= check("较轻的正常说话也能越过阈值",
              quiet >= VAD_THRESHOLD,
              "能量 " + quiet.toFixed(3) + " 低于阈值 " + VAD_THRESHOLD);

  console.log("[web-framing] " + (ok ? "PASS" : "FAIL"));
  return ok ? 0 : 1;
}

process.exit(main());
