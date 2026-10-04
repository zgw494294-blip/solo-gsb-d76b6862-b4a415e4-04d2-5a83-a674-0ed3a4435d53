"use strict";

let expId = null;
const lastRender = { a: "", b: "" };

const $ = (id) => document.getElementById(id);

class ExpGone extends Error {}

async function fetchJson(path, options) {
  const resp = await fetch(path, options);
  if (resp.status === 404) {
    // 服务端重启后旧实验已不存在：自动新建一个，再通知调用方放弃本次请求
    await newExperiment();
    throw new ExpGone();
  }
  if (!resp.ok) {
    throw new Error("HTTP " + resp.status);
  }
  return resp.json();
}

async function newExperiment() {
  const resp = await fetch("/api/experiments", { method: "POST" });
  const data = await resp.json();
  expId = data.id;
  history.replaceState(null, "", "?exp=" + expId);
  $("exp-id").textContent = expId;
  clearError("a");
  clearError("b");
  clearError("ckpt");
  await refreshStatus();
  await refreshCommitted();
}

async function refreshStatus() {
  if (!expId) return;
  const data = await fetchJson(`/api/experiments/${expId}/status`);
  renderPanel("a", data.a);
  renderPanel("b", data.b);
  renderCheckpoint(data.checkpoint);
}

async function refreshCommitted() {
  if (!expId) return;
  try {
    const data = await fetchJson(`/api/experiments/${expId}/committed`);
    $("committed").textContent = data.committed;
  } catch (e) {
    if (!(e instanceof ExpGone)) {
      // 忽略瞬时错误，下一轮轮询会恢复
    }
  }
}

function renderPanel(side, state) {
  const key = JSON.stringify(state);
  if (lastRender[side] === key) return;
  lastRender[side] = key;

  const inTx = state.in_transaction;
  $("tx-" + side).textContent = inTx ? "已开启" : "未开启";
  const dot = $("dot-" + side);
  dot.classList.toggle("on", inTx);
  dot.title = inTx ? "事务已开启" : "事务未开启";
  $("read-" + side).textContent = state.last_read === null ? "—" : state.last_read;

  const logEl = $("log-" + side);
  logEl.innerHTML = "";
  for (const entry of state.log) {
    const li = document.createElement("li");
    li.textContent = entry;
    if (entry.includes("→ 失败") || entry.includes("→ 拒绝")) {
      li.classList.add("log-error");
    }
    logEl.appendChild(li);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function renderCheckpoint(state) {
  // 只渲染服务端内存中的最近结果缓存：轮询本身不触发检查点
  const resultEl = $("ckpt-result");
  const last = state && state.last;
  if (!last) {
    resultEl.textContent = "尚未执行过检查点";
    resultEl.classList.remove("busy");
  } else {
    resultEl.textContent =
      `最近一次 ${last.mode}：busy=${last.busy}，` +
      `WAL 总帧数=${last.log_frames}，已回写帧数=${last.checkpointed}，` +
      `WAL 文件大小=${last.wal_size} 字节`;
    resultEl.classList.toggle("busy", last.busy === 1);
  }

  const logEl = $("log-ckpt");
  logEl.innerHTML = "";
  for (const entry of (state && state.log) || []) {
    const li = document.createElement("li");
    li.textContent = entry;
    if (entry.includes("失败") || entry.includes("busy=1")) {
      li.classList.add("log-error");
    }
    logEl.appendChild(li);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function setBusy(side, busy) {
  // 只禁用本栏的按钮与输入框，另一栏不受影响
  for (const el of document.querySelectorAll(
    `#panel-${side} button, #panel-${side} input`
  )) {
    el.disabled = busy;
  }
}

function showError(side, error) {
  const box = $("error-" + side);
  box.textContent = error.name
    ? `${error.name}（错误码 ${error.code}）：${error.message}`
    : error.message;
  box.hidden = false;
}

function clearError(side) {
  $("error-" + side).hidden = true;
}

async function doOp(side, op) {
  const body = { op };
  if (op === "add") {
    const raw = $(`add-input-${side}`).value.trim();
    if (!/^[+-]?\d+$/.test(raw)) {
      showError(side, { name: null, code: null, message: "请输入一个整数" });
      return;
    }
    // 大整数以十进制字符串传输避免精度丢失；安全整数范围内仍发送数字
    const big = BigInt(raw);
    body.value =
      big >= BigInt(Number.MIN_SAFE_INTEGER) &&
      big <= BigInt(Number.MAX_SAFE_INTEGER)
        ? Number(big)
        : big.toString();
  }
  setBusy(side, true);
  clearError(side);
  try {
    const data = await fetchJson(`/api/experiments/${expId}/conn/${side}/op`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderPanel(side, data.state);
    if (!data.ok && data.error) {
      showError(side, data.error);
    }
    refreshCommitted();
  } catch (e) {
    if (!(e instanceof ExpGone)) {
      showError(side, { name: null, code: null, message: "请求失败：" + e.message });
    }
  } finally {
    setBusy(side, false);
  }
}

function bindPanel(side) {
  for (const btn of document.querySelectorAll(`#panel-${side} button[data-op]`)) {
    btn.addEventListener("click", () => doOp(side, btn.dataset.op));
  }
}

function setCheckpointBusy(busy) {
  // 检查点请求进行中只禁用检查点按钮，甲、乙两栏照常可用
  $("ckpt-passive").disabled = busy;
  $("ckpt-truncate").disabled = busy;
}

async function doCheckpoint(mode) {
  setCheckpointBusy(true);
  clearError("ckpt");
  try {
    const data = await fetchJson(`/api/experiments/${expId}/checkpoint/${mode}`, {
      method: "POST",
    });
    renderCheckpoint(data.state);
    if (!data.ok && data.error) {
      showError("ckpt", data.error);
    }
  } catch (e) {
    if (!(e instanceof ExpGone)) {
      showError("ckpt", { name: null, code: null, message: "请求失败：" + e.message });
    }
  } finally {
    setCheckpointBusy(false);
  }
}

async function init() {
  bindPanel("a");
  bindPanel("b");
  $("new-exp").addEventListener("click", () => {
    newExperiment().catch(() => {});
  });
  $("refresh-committed").addEventListener("click", () => {
    refreshCommitted();
  });
  $("ckpt-passive").addEventListener("click", () => {
    doCheckpoint("PASSIVE").catch(() => {});
  });
  $("ckpt-truncate").addEventListener("click", () => {
    doCheckpoint("TRUNCATE").catch(() => {});
  });

  const params = new URLSearchParams(location.search);
  const existing = params.get("exp");
  if (existing) {
    expId = existing;
    $("exp-id").textContent = existing;
    try {
      await refreshStatus();
      await refreshCommitted();
    } catch (e) {
      // 404 时 fetchJson 已自动新建实验
    }
  } else {
    await newExperiment();
  }

  // 状态轮询只读取服务端内存状态，不执行任何 SELECT，不会提前建立快照
  setInterval(() => {
    refreshStatus().catch(() => {});
  }, 2000);
  setInterval(() => {
    refreshCommitted();
  }, 2000);
}

init();
