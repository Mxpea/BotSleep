"""Gradio 6 compact flat developer console with Three.js 3D graybox, bot launcher, stack view & config tuner."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr

from core.config import ConfigError
from core.skill_registry import SKILL_REGISTRY
from debug.web_backend import WebConsoleRuntime

COMPACT_FLAT_CSS = """
:root, .gradio-container {
  --block-padding: 6px !important;
  --block-radius: 4px !important;
  --container-radius: 4px !important;
  --input-radius: 4px !important;
  --button-small-radius: 4px !important;
  --button-large-radius: 4px !important;
  --layout-gap: 6px !important;
  --spacing-sm: 4px !important;
  --spacing-md: 6px !important;
  --spacing-lg: 8px !important;
  --text-sm: 11px !important;
  --text-md: 12px !important;
  --text-lg: 13px !important;
  --block-label-text-size: 11px !important;
  --block-label-text-weight: 400 !important;
  --button-small-text-size: 12px !important;
  --button-small-text-weight: 400 !important;
  --shadow-drop: none !important;
  --shadow-drop-lg: none !important;
  --shadow-inset: none !important;
  --block-shadow: none !important;
}
.gradio-container {
  max-width: 100% !important;
  padding: 6px 10px !important;
  font-size: 12px !important;
  font-weight: 400 !important;
}
.gradio-container * {
  box-shadow: none !important;
}
.gradio-container .tab-nav button {
  padding: 4px 10px !important;
  font-size: 12px !important;
  font-weight: 400 !important;
}
.gradio-container input,
.gradio-container textarea,
.gradio-container select {
  font-size: 12px !important;
  font-weight: 400 !important;
  padding: 4px 6px !important;
}
.gradio-container .gr-button,
.gradio-container button.sm {
  font-weight: 400 !important;
  font-size: 12px !important;
  padding: 4px 8px !important;
  min-height: 28px !important;
}
.gradio-container label > span,
.gradio-container .block-info {
  font-size: 11px !important;
  font-weight: 400 !important;
}
.gradio-container table {
  font-size: 11px !important;
  font-weight: 400 !important;
}
.gradio-container th,
.gradio-container td {
  padding: 3px 6px !important;
  font-weight: 400 !important;
}
.gradio-container .cm-editor {
  font-size: 11px !important;
}
.flat-section-title {
  font-size: 11px;
  font-weight: 400;
  color: var(--body-text-color-subdued);
  letter-spacing: 0.3px;
  margin: 2px 0 2px 0;
  padding-bottom: 2px;
  border-bottom: 1px solid var(--border-color-primary);
}
"""

THREE_VIEWPORT_HTML = """
<div id="gb-root" class="gb-root">
  <div class="gb-toolbar">
    <div class="gb-left">
      <span id="gb-renderer-badge" class="gb-badge">初始化视图…</span>
      <span id="gb-bot-badge" class="gb-mono">Pos: —</span>
      <span id="gb-voxel-badge" class="gb-mono">Voxels: 0</span>
    </div>
    <div class="gb-controls">
      <label><input type="checkbox" id="gb-follow" checked /> 跟随</label>
      <label><input type="checkbox" id="gb-slice-on" /> Y剖切</label>
      <input type="range" id="gb-slice-y" min="-64" max="320" value="320" />
      <span id="gb-slice-label" class="gb-mono">关</span>
      <button type="button" id="gb-reset-cam">重置视角</button>
    </div>
  </div>
  <div class="gb-layers">
    <label><input type="checkbox" class="gb-layer" data-layer="terrain" checked /> 地形</label>
    <label><input type="checkbox" class="gb-layer" data-layer="ore" checked /> 矿石</label>
    <label><input type="checkbox" class="gb-layer" data-layer="hazard" checked /> 危险</label>
    <label><input type="checkbox" class="gb-layer" data-layer="station" checked /> 工作站</label>
    <label><input type="checkbox" class="gb-layer" data-layer="foliage" checked /> 植被</label>
    <label><input type="checkbox" class="gb-layer" data-layer="entities" checked /> 实体</label>
    <label><input type="checkbox" class="gb-layer" data-layer="path" checked /> 轨迹/前沿</label>
    <label class="gb-radius-wrap">半径
      <select id="gb-radius">
        <option value="16">16</option>
        <option value="28" selected>28</option>
        <option value="40">40</option>
        <option value="56">56</option>
      </select>
    </label>
  </div>
  <div class="gb-stage" id="gb-stage">
    <canvas id="gb-canvas"></canvas>
    <div id="gb-hint" class="gb-hint">左拖旋转 · 右拖平移 · 滚轮缩放 · 点击方块填入坐标</div>
  </div>
  <div class="gb-footer">
    <span id="gb-picked" class="gb-mono">未选中方块</span>
    <span id="gb-source-info" class="gb-mono">记忆源: —</span>
  </div>
</div>
"""

THREE_VIEWPORT_CSS = """
.gb-root {
  background: #14181f;
  border: 1px solid #2b3240;
  border-radius: 4px;
  overflow: hidden;
  color: #dce3ed;
  font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  font-weight: 400;
}
.gb-toolbar, .gb-layers, .gb-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px 10px;
  padding: 4px 8px;
  background: #1a2029;
  border-bottom: 1px solid #2b3240;
  font-size: 11px;
  font-weight: 400;
}
.gb-layers {
  background: #161b23;
  justify-content: flex-start;
}
.gb-footer {
  border-bottom: none;
  border-top: 1px solid #2b3240;
  color: #9aa7b8;
}
.gb-left, .gb-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.gb-badge {
  background: #1f3530;
  color: #80e2c2;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 400;
}
.gb-badge.fallback {
  background: #352e1f;
  color: #f4ce82;
}
.gb-mono {
  font-family: Consolas, "Cascadia Code", ui-monospace, monospace;
}
.gb-controls button {
  background: #202733;
  color: #dce3ed;
  border: 1px solid #343f52;
  border-radius: 3px;
  padding: 2px 7px;
  font-size: 11px;
  font-weight: 400;
  cursor: pointer;
}
.gb-controls button:hover {
  border-color: #80e2c2;
  color: #80e2c2;
}
.gb-stage {
  position: relative;
  width: 100%;
  height: 390px;
  background: #10141a;
}
#gb-canvas {
  width: 100%;
  height: 100%;
  display: block;
  cursor: grab;
}
#gb-canvas:active {
  cursor: grabbing;
}
.gb-hint {
  position: absolute;
  left: 6px;
  bottom: 6px;
  background: rgba(16, 20, 26, 0.78);
  border: 1px solid #2b3240;
  border-radius: 3px;
  padding: 2px 6px;
  font-size: 10px;
  color: #9aa7b8;
  pointer-events: none;
}
.gb-layers label {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  cursor: pointer;
  user-select: none;
}
.gb-layers select {
  background: #202733;
  color: #dce3ed;
  border: 1px solid #343f52;
  border-radius: 3px;
  padding: 1px 4px;
  font-size: 11px;
}
"""

THREE_VIEWPORT_JS = r"""
(() => {
  const stage = element.querySelector('#gb-stage');
  const canvas = element.querySelector('#gb-canvas');
  const badge = element.querySelector('#gb-renderer-badge');
  const botBadge = element.querySelector('#gb-bot-badge');
  const voxelBadge = element.querySelector('#gb-voxel-badge');
  const pickedEl = element.querySelector('#gb-picked');
  const sourceEl = element.querySelector('#gb-source-info');
  const followCb = element.querySelector('#gb-follow');
  const sliceOnCb = element.querySelector('#gb-slice-on');
  const sliceSlider = element.querySelector('#gb-slice-y');
  const sliceLabel = element.querySelector('#gb-slice-label');
  const resetBtn = element.querySelector('#gb-reset-cam');
  const radiusSel = element.querySelector('#gb-radius');

  const layerState = {
    terrain: true, ore: true, hazard: true, station: true, foliage: true, entities: true, path: true
  };
  element.querySelectorAll('.gb-layer').forEach(cb => {
    cb.addEventListener('change', () => {
      layerState[cb.dataset.layer] = cb.checked;
      renderLatest();
    });
  });

  let latestScene = null;
  let mode = 'init';
  let fetching = false;
  let sliceInitialized = false;

  const camState = {
    target: [0, 64, 0],
    yaw: 0.75,
    pitch: 0.62,
    dist: 24,
    initialized: false
  };

  function loadScript(src, timeoutMs = 4500) {
    return new Promise((resolve, reject) => {
      if (window.THREE) return resolve(window.THREE);
      const existing = Array.from(document.scripts).find(s => s.src === src);
      if (existing && window.THREE) return resolve(window.THREE);
      const script = document.createElement('script');
      script.src = src;
      script.async = true;
      const timer = setTimeout(() => reject(new Error('timeout')), timeoutMs);
      script.onload = () => {
        clearTimeout(timer);
        if (window.THREE) resolve(window.THREE);
        else reject(new Error('THREE missing'));
      };
      script.onerror = () => { clearTimeout(timer); reject(new Error('load error')); };
      document.head.appendChild(script);
    });
  }

  async function ensureThree() {
    if (window.THREE) return window.THREE;
    const urls = [
      'https://cdn.jsdelivr.net/npm/three@0.149.0/build/three.min.js',
      'https://unpkg.com/three@0.149.0/build/three.min.js'
    ];
    for (const url of urls) {
      try {
        return await loadScript(url);
      } catch (_) {}
    }
    return null;
  }

  let dragMode = null;
  let dragStart = [0, 0];
  let dragMoved = false;

  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('mousedown', e => {
    dragMode = e.button === 2 ? 'pan' : 'orbit';
    dragStart = [e.clientX, e.clientY];
    dragMoved = false;
  });
  window.addEventListener('mouseup', e => {
    if (dragMode === 'orbit' && !dragMoved) {
      handlePick(e.clientX, e.clientY);
    }
    dragMode = null;
  });
  window.addEventListener('mousemove', e => {
    if (!dragMode) return;
    const dx = e.clientX - dragStart[0];
    const dy = e.clientY - dragStart[1];
    if (Math.abs(dx) + Math.abs(dy) > 3) dragMoved = true;
    dragStart = [e.clientX, e.clientY];
    if (dragMode === 'orbit') {
      camState.yaw -= dx * 0.008;
      camState.pitch = Math.max(-1.45, Math.min(1.45, camState.pitch + dy * 0.008));
    } else if (dragMode === 'pan') {
      followCb.checked = false;
      const scale = camState.dist * 0.0025;
      const cosY = Math.cos(camState.yaw), sinY = Math.sin(camState.yaw);
      camState.target[0] -= (cosY * dx - sinY * dy) * scale;
      camState.target[2] -= (sinY * dx + cosY * dy) * scale;
    }
    updateCameraTransform();
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const factor = e.deltaY > 0 ? 1.12 : 0.89;
    camState.dist = Math.max(4, Math.min(120, camState.dist * factor));
    updateCameraTransform();
  }, { passive: false });

  resetBtn.addEventListener('click', () => {
    followCb.checked = true;
    camState.yaw = 0.75;
    camState.pitch = 0.62;
    camState.dist = 24;
    if (latestScene && latestScene.bot && latestScene.bot.pos) {
      camState.target = [...latestScene.bot.pos];
    }
    updateCameraTransform();
  });

  sliceOnCb.addEventListener('change', () => {
    sliceLabel.textContent = sliceOnCb.checked ? `Y<=${sliceSlider.value}` : '关';
    renderLatest();
  });
  sliceSlider.addEventListener('input', () => {
    sliceOnCb.checked = true;
    sliceLabel.textContent = `Y<=${sliceSlider.value}`;
    renderLatest();
  });

  let THREE_LIB = null;
  let renderer = null, scene = null, camera = null;
  let voxelGroup = null, dynamicGroup = null, botGroup = null, gridHelper = null;
  let activeVoxelMesh = null, activeVoxelList = [];
  let raycaster = null;
  let sharedBoxGeo = null;

  function initThree(THREE) {
    THREE_LIB = THREE;
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(55, 1, 0.1, 500);
    raycaster = new THREE.Raycaster();

    const ambient = new THREE.AmbientLight(0xd9e6f5, 0.72);
    scene.add(ambient);
    const sun = new THREE.DirectionalLight(0xffffff, 0.75);
    sun.position.set(24, 48, 20);
    scene.add(sun);
    const fill = new THREE.DirectionalLight(0x88b4e6, 0.3);
    fill.position.set(-20, -15, -24);
    scene.add(fill);

    gridHelper = new THREE.GridHelper(64, 64, 0x35506d, 0x1c2836);
    scene.add(gridHelper);

    voxelGroup = new THREE.Group();
    dynamicGroup = new THREE.Group();
    botGroup = new THREE.Group();
    scene.add(voxelGroup);
    scene.add(dynamicGroup);
    scene.add(botGroup);

    const bodyGeo = new THREE.BoxGeometry(0.65, 1.75, 0.65);
    const bodyMat = new THREE.MeshLambertMaterial({ color: 0x52e0b6 });
    const bodyMesh = new THREE.Mesh(bodyGeo, bodyMat);
    bodyMesh.position.y = 0.875;
    botGroup.add(bodyMesh);

    const arrowGeo = new THREE.ConeGeometry(0.24, 0.85, 8);
    arrowGeo.rotateX(Math.PI / 2);
    const arrowMat = new THREE.MeshBasicMaterial({ color: 0xffe066 });
    const arrowMesh = new THREE.Mesh(arrowGeo, arrowMat);
    arrowMesh.position.set(0, 1.5, 0.75);
    botGroup.add(arrowMesh);

    const resize = () => {
      const w = stage.clientWidth || 640;
      const h = stage.clientHeight || 390;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    if (window.ResizeObserver) {
      new ResizeObserver(resize).observe(stage);
    } else {
      window.addEventListener('resize', resize);
    }

    mode = 'webgl';
    badge.textContent = 'WebGL 3D';
    badge.className = 'gb-badge';

    const animate = () => {
      requestAnimationFrame(animate);
      if (renderer && scene && camera) {
        renderer.render(scene, camera);
      }
    };
    animate();
  }

  function updateCameraTransform() {
    if (mode !== 'webgl' || !camera) {
      render2DFallback();
      return;
    }
    const [tx, ty, tz] = camState.target;
    const cosP = Math.cos(camState.pitch);
    const cx = tx + camState.dist * cosP * Math.sin(camState.yaw);
    const cy = ty + camState.dist * Math.sin(camState.pitch) + 1.2;
    const cz = tz + camState.dist * cosP * Math.cos(camState.yaw);
    camera.position.set(cx, cy, cz);
    camera.lookAt(tx, ty + 1.0, tz);
  }

  function clearGroup(grp) {
    while (grp.children.length > 0) {
      const child = grp.children[0];
      grp.remove(child);
      if (child.geometry && child.geometry !== sharedBoxGeo) child.geometry.dispose();
      if (child.material) {
        if (Array.isArray(child.material)) child.material.forEach(m => m.dispose());
        else child.material.dispose();
      }
    }
  }

  function renderWebGL(data) {
    const THREE = THREE_LIB;
    if (!sharedBoxGeo) sharedBoxGeo = new THREE.BoxGeometry(0.96, 0.96, 0.96);

    clearGroup(voxelGroup);
    clearGroup(dynamicGroup);
    activeVoxelMesh = null;
    activeVoxelList = [];

    const yLimit = sliceOnCb.checked ? Number(sliceSlider.value) : 9999;
    const filtered = (data.voxels || []).filter(v => layerState[v.cat] !== false && v.y <= yLimit);
    activeVoxelList = filtered;

    if (filtered.length > 0) {
      const mat = new THREE.MeshLambertMaterial({ color: 0xffffff, transparent: true, opacity: 0.9 });
      const inst = new THREE.InstancedMesh(sharedBoxGeo, mat, filtered.length);
      const dummy = new THREE.Object3D();
      const col = new THREE.Color();
      for (let i = 0; i < filtered.length; i++) {
        const v = filtered[i];
        dummy.position.set(v.x + 0.5, v.y + 0.5, v.z + 0.5);
        dummy.scale.setScalar(v.cat === 'foliage' ? 0.88 : 1.0);
        dummy.updateMatrix();
        inst.setMatrixAt(i, dummy.matrix);
        col.set(v.color || '#7b8491');
        inst.setColorAt(i, col);
      }
      inst.instanceMatrix.needsUpdate = true;
      if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
      voxelGroup.add(inst);
      activeVoxelMesh = inst;
    }

    const bot = data.bot || {};
    const bpos = bot.pos || [0, 64, 0];
    botGroup.position.set(bpos[0], bpos[1], bpos[2]);
    const yaw = typeof bot.yaw === 'number' ? bot.yaw : 0;
    botGroup.rotation.y = yaw;
    gridHelper.position.set(Math.floor(bpos[0]), Math.floor(bpos[1]) - 0.02, Math.floor(bpos[2]));

    if (layerState.entities) {
      for (const ent of (data.entities || [])) {
        if (!ent.pos || ent.pos[1] > yLimit + 2) continue;
        const isHostile = ent.type === 'hostile';
        const isItem = ent.type === 'item' || ent.name === 'item';
        const geo = new THREE.BoxGeometry(isItem ? 0.35 : 0.65, isItem ? 0.35 : 1.35, isItem ? 0.35 : 0.65);
        const mat = new THREE.MeshBasicMaterial({
          color: isHostile ? 0xff4d6d : isItem ? 0xffe066 : 0x70c7ff,
          wireframe: !isHostile
        });
        const m = new THREE.Mesh(geo, mat);
        m.position.set(ent.pos[0], ent.pos[1] + (isItem ? 0.25 : 0.7), ent.pos[2]);
        dynamicGroup.add(m);
      }
    }

    if (layerState.path) {
      const trailPts = (data.trail || []).filter(p => p[1] <= yLimit + 3).map(p => new THREE.Vector3(p[0], p[1] + 0.15, p[2]));
      if (trailPts.length >= 2) {
        const geo = new THREE.BufferGeometry().setFromPoints(trailPts);
        const mat = new THREE.LineBasicMaterial({ color: 0x52e0b6 });
        dynamicGroup.add(new THREE.Line(geo, mat));
      }
      for (const fn of (data.frontiers || []).slice(0, 120)) {
        if (!fn.pos || fn.pos[1] > yLimit + 2) continue;
        const geo = new THREE.BoxGeometry(0.45, 0.45, 0.45);
        const mat = new THREE.MeshBasicMaterial({ color: 0xb788ff, wireframe: true });
        const m = new THREE.Mesh(geo, mat);
        m.position.set(fn.pos[0] + 0.5, fn.pos[1] + 0.5, fn.pos[2] + 0.5);
        dynamicGroup.add(m);
      }
      for (const pos of (data.stand_candidates || [])) {
        if (pos[1] > yLimit + 2) continue;
        const geo = new THREE.BoxGeometry(0.42, 0.12, 0.42);
        const mat = new THREE.MeshBasicMaterial({ color: 0x70f5a8 });
        const m = new THREE.Mesh(geo, mat);
        m.position.set(pos[0], pos[1] + 0.08, pos[2]);
        dynamicGroup.add(m);
      }
    }
  }

  function render2DFallback() {
    if (mode !== '2d' || !latestScene) return;
    const w = stage.clientWidth || 640;
    const h = stage.clientHeight || 390;
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#10141a';
    ctx.fillRect(0, 0, w, h);

    const scale = Math.max(4, Math.min(36, 480 / camState.dist));
    const [tx, , tz] = camState.target;
    const yLimit = sliceOnCb.checked ? Number(sliceSlider.value) : 9999;

    const toScreen = (wx, wz) => [
      w / 2 + (wx - tx) * scale,
      h / 2 + (wz - tz) * scale
    ];

    const voxels = (latestScene.voxels || [])
      .filter(v => layerState[v.cat] !== false && v.y <= yLimit)
      .sort((a, b) => a.y - b.y);
    for (const v of voxels) {
      const [sx, sy] = toScreen(v.x, v.z);
      ctx.fillStyle = v.color || '#7b8491';
      ctx.globalAlpha = v.cat === 'terrain' ? 0.55 : 0.92;
      ctx.fillRect(sx, sy, Math.max(2, scale - 1), Math.max(2, scale - 1));
    }
    ctx.globalAlpha = 1.0;

    if (layerState.path && (latestScene.trail || []).length > 1) {
      ctx.strokeStyle = '#52e0b6';
      ctx.lineWidth = 2;
      ctx.beginPath();
      latestScene.trail.forEach((p, idx) => {
        const [sx, sy] = toScreen(p[0], p[2]);
        if (idx === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
      });
      ctx.stroke();
    }

    if (layerState.entities) {
      for (const ent of (latestScene.entities || [])) {
        const [sx, sy] = toScreen(ent.pos[0], ent.pos[2]);
        ctx.fillStyle = ent.type === 'hostile' ? '#ff4d6d' : '#70c7ff';
        ctx.beginPath();
        ctx.arc(sx, sy, Math.max(3, scale * 0.25), 0, Math.PI * 2);
        ctx.fill();
      }
    }

    const bpos = (latestScene.bot || {}).pos || [0, 64, 0];
    const [bx, by] = toScreen(bpos[0], bpos[2]);
    ctx.fillStyle = '#52e0b6';
    ctx.beginPath();
    ctx.arc(bx, by, Math.max(5, scale * 0.35), 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  function handlePick(clientX, clientY) {
    if (!latestScene) return;
    if (mode === 'webgl' && raycaster && activeVoxelMesh && camera) {
      const rect = canvas.getBoundingClientRect();
      const ndc = new THREE_LIB.Vector2(
        ((clientX - rect.left) / rect.width) * 2 - 1,
        -((clientY - rect.top) / rect.height) * 2 + 1
      );
      raycaster.setFromCamera(ndc, camera);
      const hits = raycaster.intersectObject(activeVoxelMesh, false);
      if (hits.length > 0 && hits[0].instanceId != null) {
        const v = activeVoxelList[hits[0].instanceId];
        if (v) {
          pickedEl.textContent = `选中: ${v.name} @ [${v.x}, ${v.y}, ${v.z}] (${v.src})`;
          trigger('click', { x: v.x, y: v.y + 1, z: v.z, name: v.name });
        }
      }
    } else if (mode === '2d') {
      const rect = canvas.getBoundingClientRect();
      const scale = Math.max(4, Math.min(36, 480 / camState.dist));
      const [tx, ty, tz] = camState.target;
      const wx = Math.floor(tx + (clientX - rect.left - rect.width / 2) / scale);
      const wz = Math.floor(tz + (clientY - rect.top - rect.height / 2) / scale);
      const yLimit = sliceOnCb.checked ? Number(sliceSlider.value) : 9999;
      const col = (latestScene.voxels || []).filter(v => v.x === wx && v.z === wz && v.y <= yLimit).sort((a, b) => b.y - a.y);
      const top = col[0];
      const py = top ? top.y + 1 : Math.round(ty);
      pickedEl.textContent = top ? `选中: ${top.name} @ [${wx}, ${top.y}, ${wz}]` : `地面坐标 @ [${wx}, ${py}, ${wz}]`;
      trigger('click', { x: wx, y: py, z: wz, name: top ? top.name : 'ground' });
    }
  }

  function renderLatest() {
    if (!latestScene) return;
    const bot = latestScene.bot || {};
    const pos = bot.pos || [0, 64, 0];
    if (!camState.initialized || followCb.checked) {
      camState.target = [pos[0], pos[1], pos[2]];
      camState.initialized = true;
    }
    const yb = latestScene.y_bounds || { min: 0, max: 128 };
    sliceSlider.min = String(yb.min - 4);
    sliceSlider.max = String(yb.max + 4);
    if (!sliceInitialized) {
      sliceSlider.value = String(yb.max + 4);
      sliceLabel.textContent = sliceOnCb.checked ? `Y<=${sliceSlider.value}` : '关';
      sliceInitialized = true;
    }
    botBadge.textContent = `Pos: [${pos[0]}, ${pos[1]}, ${pos[2]}] (${bot.dimension || '?'})`;
    voxelBadge.textContent = `Voxels: ${(latestScene.voxels || []).length}/${latestScene.total_cached || 0}`;
    const ms = latestScene.manual_scan ? ` · 扫描 ${latestScene.manual_scan.solid_count}` : '';
    sourceEl.textContent = `记忆源: ${latestScene.world_source || '实时观测'}${ms}`;

    if (mode === 'webgl') {
      renderWebGL(latestScene);
      updateCameraTransform();
    } else {
      render2DFallback();
    }
  }

  async function pollScene() {
    if (fetching || document.hidden) return;
    fetching = true;
    try {
      const radius = Number(radiusSel.value || 28);
      const data = await server.fetch_scene3d({ radius });
      if (data && typeof data === 'object') {
        latestScene = data;
        renderLatest();
      }
    } catch (_) {
    } finally {
      fetching = false;
    }
  }

  (async () => {
    const THREE = await ensureThree();
    if (THREE) {
      initThree(THREE);
    } else {
      mode = '2d';
      badge.textContent = '2D 离线降级';
      badge.className = 'gb-badge fallback';
    }
    await pollScene();
    setInterval(pollScene, 550);
  })();
})();
"""


def _esc(val: Any) -> str:
    return html_lib.escape("" if val is None else str(val))


def _format_status_banner(report: Dict[str, Any]) -> str:
    host_mode = report.get("host_mode", "unknown")
    scope = report.get("scope", {})
    mon = report.get("monitor", {})
    obs_status = (report.get("observation") or {}).get("status") or {}
    ctrl = report.get("controller") or {}
    gate = report.get("model_gate") or {}
    llm = mon.get("llm") or {}
    rt = report.get("runtime_code") or {}

    mode_label = {
        "host": "HOST",
        "readonly_attach": "READONLY_ATTACH",
        "disconnected": "DISCONNECTED",
        "stopped": "STOPPED",
    }.get(host_mode, str(host_mode))

    pos = obs_status.get("pos")
    pos_str = f"[{pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}]" if isinstance(pos, list) and len(pos) == 3 else "—"
    hp = obs_status.get("health", "—")
    food = obs_status.get("food", "—")
    dim = obs_status.get("dimension", "—")
    day_txt = "白天" if obs_status.get("is_day") is True else ("夜间" if obs_status.get("is_day") is False else "—")
    phase = rt.get("phase") or ctrl.get("phase") or (mon.get("run") or {}).get("phase") or "idle"
    phase_s = rt.get("phase_elapsed_s", 0)
    steps = ctrl.get("steps", (mon.get("run") or {}).get("steps", 0))
    paused = ctrl.get("paused", (mon.get("run") or {}).get("paused", False))
    busy = ctrl.get("busy", obs_status.get("busy", False))
    pending = report.get("pending")
    gate_state = gate.get("state", "idle")
    actual_model = llm.get("actual_model") or "未确认"
    route_model = llm.get("request_model") or "—"

    alerts: List[str] = []
    if report.get("connect_error"):
        alerts.append(f"连接异常: {_esc(report['connect_error'])}")
    if report.get("restart_required"):
        alerts.append("结构配置已改，请热重启")
    if pending is not None:
        alerts.append("存在 pending 动作意图")
    if gate_state == "uncertain":
        alerts.append("模型门禁 UNCERTAIN")
    if paused:
        reason = ctrl.get("pause_reason") or (mon.get("run") or {}).get("pause_reason") or "paused"
        alerts.append(f"AI 已暂停 ({_esc(reason)})")

    alert_html = (
        "<div style='margin-top:4px;padding:3px 6px;border-radius:3px;border:1px solid #8a6d3b;"
        "background:rgba(244,206,130,0.08);font-size:11px;font-weight:400;'>"
        + " · ".join(alerts)
        + "</div>"
    ) if alerts else ""

    return (
        "<div style='padding:5px 8px;border-radius:4px;border:1px solid var(--border-color-primary);"
        "background:var(--background-fill-secondary);font-size:11px;font-weight:400;line-height:1.45;'>"
        "<div style='display:flex;flex-wrap:wrap;gap:6px 14px;justify-content:space-between;align-items:center;'>"
        f"<div>状态: <code>{_esc(mode_label)}</code> · "
        f"<code>{_esc(scope.get('username'))}@{_esc(scope.get('world_id'))}</code> "
        f"({_esc(scope.get('mode'))}/{_esc(scope.get('objective'))})</div>"
        f"<div>位置: <code>{pos_str}</code> ({_esc(dim)} / {day_txt}) · HP <code>{hp}/20</code> · Food <code>{food}/20</code></div>"
        f"<div>阶段: <code>{_esc(phase)}</code> ({phase_s}s) · 步数 <code>{steps}</code> · "
        f"Busy <code>{bool(busy)}</code> · Slot <code>{_esc(gate_state)}</code> · "
        f"模型 <code>{_esc(actual_model)}</code> (路由 {_esc(route_model)})</div>"
        f"</div>{alert_html}</div>"
    )


def _format_live_cockpit_cards(report: Dict[str, Any], events: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Render three compact flat live-info cards directly below the 3D viewport on Tab 1."""
    rt = report.get("runtime_code") or {}
    boards = report.get("boards") or {}
    obs = report.get("observation") or {}
    decision = rt.get("active_decision") or boards.get("decision") or {}
    task_exec = rt.get("task_execution") or boards.get("task_execution") or {}
    last_res = boards.get("last_result") or {}
    playbook = boards.get("playbook") or {}

    # Card A: Current Action & Live Python Code
    dec_name = decision.get("name") or "—"
    dec_goal = decision.get("goal") or "—"
    dec_reason = decision.get("reason") or "—"
    dec_args = json.dumps(decision.get("args", {}), ensure_ascii=False)[:140] if decision else "{}"
    task_line = "—"
    if isinstance(task_exec, dict) and task_exec.get("task"):
        prog = task_exec.get("progress") or {}
        task_line = (
            f"{task_exec.get('task')} [{task_exec.get('status')}] "
            f"{prog.get('current', 0)}/{prog.get('target', '?')} {prog.get('unit', '')} "
            f"(step {task_exec.get('steps', 0)}/{task_exec.get('max_steps', '?')})"
        )
    threads = rt.get("threads") or []
    code_rows: List[str] = []
    for th in threads:
        cur = th.get("current") or {}
        if cur:
            code_rows.append(
                f"<div><code>{_esc(th['thread'])}</code> → "
                f"<code>{_esc(cur.get('file'))}:{cur.get('line')}</code> {_esc(cur.get('function'))}()<br/>"
                f"<span style='opacity:0.8;font-family:monospace;'>{_esc(cur.get('code', '')[:90])}</span></div>"
            )
    if not code_rows:
        code_rows.append("<div style='opacity:0.7;'>暂无活跃工作线程栈</div>")

    card_a = (
        "<div style='padding:6px 8px;border:1px solid var(--border-color-primary);border-radius:4px;"
        "font-size:11px;font-weight:400;line-height:1.45;'>"
        "<div style='opacity:0.75;margin-bottom:3px;'>正在执行 / 实时代码位置</div>"
        f"<div>决策: <code>{_esc(dec_name)}</code> ({_esc(dec_goal)})</div>"
        f"<div style='opacity:0.85;'>参数: <code>{_esc(dec_args)}</code></div>"
        f"<div style='opacity:0.85;'>原因: {_esc(str(dec_reason)[:100])}</div>"
        f"<div>长任务: <code>{_esc(task_line)}</code></div>"
        "<div style='margin-top:4px;border-top:1px solid var(--border-color-primary);padding-top:3px;'>"
        + "".join(code_rows)
        + "</div></div>"
    )

    # Card B: Last Result, Playbook & Inventory Summary
    res_ok = last_res.get("success")
    res_msg = last_res.get("message") or "—"
    res_err = last_res.get("error_code") or "—"
    pb_stage = playbook.get("stage") or "—"
    pb_missing = ", ".join(playbook.get("missing") or []) or "—"
    counts = obs.get("counts") or {}
    inv_items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:16]
    inv_txt = " · ".join(f"{_esc(k)}×{v}" for k, v in inv_items) if inv_items else "（未观测或空）"
    ents = obs.get("entities") or []
    hostile_cnt = sum(1 for e in ents if isinstance(e, dict) and e.get("type") == "hostile")

    card_b = (
        "<div style='padding:6px 8px;border:1px solid var(--border-color-primary);border-radius:4px;"
        "font-size:11px;font-weight:400;line-height:1.45;'>"
        "<div style='opacity:0.75;margin-bottom:3px;'>最近结果 · 阶段 · 背包摘要</div>"
        f"<div>结果: <code>ok={_esc(res_ok)}</code> code=<code>{_esc(res_err)}</code></div>"
        f"<div style='opacity:0.85;'>消息: {_esc(str(res_msg)[:120])}</div>"
        f"<div>阶段建议: <code>{_esc(pb_stage)}</code> · 缺口: {_esc(pb_missing[:90])}</div>"
        f"<div>周边实体: <code>{len(ents)}</code> (敌对 <code>{hostile_cnt}</code>)</div>"
        f"<div style='margin-top:4px;border-top:1px solid var(--border-color-primary);padding-top:3px;'>"
        f"背包: {inv_txt}</div></div>"
    )

    # Card C: Compact Recent Events Feed (last 8)
    ev_lines: List[str] = []
    for ev in reversed(events[-8:]):
        ts = str(ev.get("timestamp", ""))[11:19]
        lvl = str(ev.get("level", "INFO"))
        ev_name = str(ev.get("event", ""))
        summary = json.dumps(ev.get("data", {}), ensure_ascii=False)[:75]
        ev_lines.append(
            f"<div style='white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>"
            f"<code>{ts}</code> [{_esc(lvl)}] <code>{_esc(ev_name)}</code> {_esc(summary)}</div>"
        )
    if not ev_lines:
        ev_lines.append("<div style='opacity:0.7;'>等待运行事件…</div>")

    card_c = (
        "<div style='padding:6px 8px;border:1px solid var(--border-color-primary);border-radius:4px;"
        "font-size:11px;font-weight:400;line-height:1.45;'>"
        "<div style='opacity:0.75;margin-bottom:3px;'>最近事件流 (最新 8 条)</div>"
        + "".join(ev_lines)
        + "</div>"
    )

    return card_a, card_b, card_c


def _format_runtime_code(rt: Dict[str, Any], boards: Dict[str, Any]) -> Tuple[str, str, str]:
    decision = rt.get("active_decision") or boards.get("decision") or {}
    task_exec = rt.get("task_execution") or boards.get("task_execution")
    threads = rt.get("threads") or []

    stack_lines = [
        f"阶段: `{rt.get('phase', 'idle')}` ({rt.get('phase_elapsed_s', 0)}s | busy=`{rt.get('busy')}` | model_pending=`{rt.get('model_pending')}`)",
    ]
    if decision:
        stack_lines.append(
            f"- 决策: `{decision.get('name')}` (`goal={decision.get('goal')}`) — {decision.get('reason', '')}"
        )
        stack_lines.append(f"- 参数: `{json.dumps(decision.get('args', {}), ensure_ascii=False)}`")
    if not threads:
        stack_lines.append("\n当前无活跃后台工作线程（或在只读附加模式下运行于外部进程）。")
    for th in threads:
        cur = th.get("current") or {}
        stack_lines.append(
            f"\n**{th['thread']}** (tid={th['tid']})"
            + (f" — `{cur.get('file')}:{cur.get('line')}` `{cur.get('function')}()`" if cur else "")
        )
        if cur and cur.get("code"):
            stack_lines.append(f"```python\n{cur['code']}\n```")
        frames_txt = "\n".join(
            f"{'-> ' if f.get('in_repo') else '   '}{f['file']}:{f['line']} in {f['function']}() | {f['code']}"
            for f in th.get("frames", [])
        )
        if frames_txt:
            stack_lines.append(
                f"<details><summary>调用栈 ({len(th.get('frames', []))} 帧)</summary>\n\n```text\n{frames_txt}\n```\n</details>"
            )

    task_json = json.dumps(task_exec, ensure_ascii=False, indent=2) if task_exec else "{}"
    last_res = boards.get("last_result")
    res_json = json.dumps(last_res, ensure_ascii=False, indent=2) if last_res else "{}"
    return "\n".join(stack_lines), task_json, res_json


def _format_chat_log(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "（暂无聊天/系统消息）"
    return "\n".join(
        f"[{r.get('timestamp', '')}] ({r.get('type', '')}) {r.get('text', '')}"
        for r in rows[-40:]
    )


def _format_events_rows(events: List[Dict[str, Any]]) -> List[List[str]]:
    rows: List[List[str]] = []
    for ev in reversed(events):
        rows.append([
            str(ev.get("seq", "")),
            str(ev.get("timestamp", ""))[11:23],
            str(ev.get("level", "")),
            str(ev.get("module", "")),
            str(ev.get("event", "")),
            json.dumps(ev.get("data", {}), ensure_ascii=False)[:220],
        ])
    return rows


def build_demo(runtime: WebConsoleRuntime) -> gr.Blocks:
    """Construct the compact, flat Gradio 6 developer workbench bound to WebConsoleRuntime."""
    init_cfg = runtime.get_editable_settings()
    skill_choices = sorted(
        name
        for name, spec in SKILL_REGISTRY.items()
        if name == "wait" or spec.runner_slot not in {"meta", "autonomy", "competition"}
    )

    def fetch_scene3d(payload: Any = None) -> Dict[str, Any]:
        radius = 28
        if isinstance(payload, dict) and payload.get("radius") is not None:
            try:
                radius = int(payload["radius"])
            except (TypeError, ValueError):
                radius = 28
        return runtime.get_scene_3d(radius=radius)

    with gr.Blocks(title="BotSleep · Developer Workbench", fill_width=True, analytics_enabled=False) as demo:
        banner_html = gr.HTML(value=_format_status_banner(runtime.status_report()))

        with gr.Tabs():
            # ================================================================
            # Tab 1: High-Density Live Cockpit (Launcher + 3D + Live Info + Controls)
            # ================================================================
            with gr.Tab("总览 · 3D 灰模与实时控制台"):
                # Top compact Bot Launcher & Lifecycle bar
                with gr.Row():
                    launch_username = gr.Textbox(
                        value=init_cfg["agent"]["name"], label="Bot 账号", scale=2
                    )
                    launch_mode = gr.Dropdown(
                        ["survival", "sleep"], value=init_cfg["agent"]["mode"], label="模式", scale=1
                    )
                    launch_objective = gr.Dropdown(
                        ["starter", "daily", "competition"], value=init_cfg["survival"]["objective"], label="目标", scale=1
                    )
                    btn_join_game = gr.Button("启动 / 加入游戏", variant="primary", size="sm", scale=1)
                    btn_join_paused = gr.Button("暂停加入", size="sm", scale=1)
                    btn_disconnect = gr.Button("断开 / 登出", size="sm", scale=1)
                    btn_pause = gr.Button("暂停 AI", size="sm", scale=1)
                    btn_resume = gr.Button("恢复 AI", size="sm", scale=1)
                    btn_step = gr.Button("单步", size="sm", scale=1)
                    btn_estop = gr.Button("急停 STOP", variant="stop", size="sm", scale=1)

                with gr.Row():
                    ctrl_feedback = gr.Textbox(
                        placeholder="控制/连接反馈...", show_label=False, interactive=False, lines=1, scale=6
                    )
                    btn_ack_llm = gr.Button("确认模型空闲", size="sm", scale=1)
                    force_override = gr.Checkbox(label="开发者强制接管", value=False, scale=2)

                with gr.Row():
                    # Left side: 3D Graybox + Live 3-column Info Strip
                    with gr.Column(scale=7):
                        viewport_3d = gr.HTML(
                            value="",
                            html_template=THREE_VIEWPORT_HTML,
                            css_template=THREE_VIEWPORT_CSS,
                            js_on_load=THREE_VIEWPORT_JS,
                            server_functions=[fetch_scene3d],
                        )
                        with gr.Row():
                            scan_rx = gr.Slider(1, 6, value=4, step=1, label="3D扫描XZ", scale=2)
                            scan_ry = gr.Slider(1, 5, value=3, step=1, label="Y", scale=2)
                            scan_btn = gr.Button("局部 3D 采样", size="sm", scale=1)
                            scan_result = gr.Textbox(
                                placeholder="3D 采样结果...", show_label=False, interactive=False, lines=1, scale=4
                            )

                        # Live 3-column info cards right on screen!
                        init_rep = runtime.status_report()
                        init_evs = runtime.get_events(limit=8)
                        c_a, c_b, c_c = _format_live_cockpit_cards(init_rep, init_evs)
                        with gr.Row():
                            live_card_code = gr.HTML(value=c_a)
                            live_card_state = gr.HTML(value=c_b)
                            live_card_events = gr.HTML(value=c_c)

                    # Right side: Compact Movement, Skill, Chat
                    with gr.Column(scale=4):
                        gr.HTML("<div class='flat-section-title'>手动移动与目标坐标</div>")
                        with gr.Row():
                            move_ms = gr.Slider(100, 2000, value=400, step=50, label="步进ms", scale=3)
                            move_sprint = gr.Checkbox(label="疾跑", value=False, scale=1)
                            move_sneak = gr.Checkbox(label="潜行", value=False, scale=1)
                        with gr.Row():
                            btn_fwd = gr.Button("↑前", size="sm", min_width=40)
                            btn_back = gr.Button("↓后", size="sm", min_width=40)
                            btn_left = gr.Button("←左", size="sm", min_width=40)
                            btn_right = gr.Button("→右", size="sm", min_width=40)
                            btn_jump = gr.Button("⤴跳", size="sm", min_width=40)
                        with gr.Row():
                            target_x = gr.Number(label="X", value=0)
                            target_y = gr.Number(label="Y", value=64)
                            target_z = gr.Number(label="Z", value=0)
                            target_range = gr.Number(label="R", value=1.0)
                        with gr.Row():
                            btn_moveto = gr.Button("寻路移动", size="sm")
                            btn_lookat = gr.Button("看向坐标", size="sm")

                        gr.HTML("<div class='flat-section-title'>快捷底层技能</div>")
                        with gr.Row():
                            skill_dd = gr.Dropdown(choices=skill_choices, value="wait", label="技能", scale=2)
                            skill_args = gr.Textbox(value='{"seconds": 1}', label="JSON", scale=3)
                            btn_run_skill = gr.Button("执行", size="sm", scale=1)

                        gr.HTML("<div class='flat-section-title'>游戏内聊天</div>")
                        chat_log = gr.Textbox(show_label=False, lines=5, interactive=False)
                        with gr.Row():
                            chat_input = gr.Textbox(placeholder="发送聊天消息...", show_label=False, scale=4)
                            btn_send_chat = gr.Button("发送", variant="primary", size="sm", scale=1)

            # ================================================================
            # Tab 2: AI Decisions, Full Python Call Stack & Event Timeline
            # ================================================================
            with gr.Tab("AI 决策 · 代码调用栈 · 事件流"):
                with gr.Row():
                    with gr.Column(scale=6):
                        stack_md = gr.Markdown("等待线程堆栈采样…")
                    with gr.Column(scale=6):
                        gr.HTML("<div class='flat-section-title'>程序长任务 (task_execution) & 最近结果</div>")
                        task_exec_code = gr.Code(language="json", value="{}", lines=8)
                        last_res_code = gr.Code(language="json", value="{}", lines=6)

                with gr.Row():
                    llm_req_code = gr.Code(label="LLM_REQUEST (脱敏)", language="json", value="{}", lines=9)
                    llm_resp_code = gr.Code(label="LLM_RESPONSE / ERROR (脱敏)", language="json", value="{}", lines=9)

                with gr.Row():
                    ev_level = gr.Dropdown(choices=["ALL", "INFO", "WARN", "ERROR"], value="ALL", label="级别")
                    ev_module = gr.Dropdown(
                        choices=["ALL", "survival", "llm", "main", "web_console", "auto_sleep", "journal"],
                        value="ALL",
                        label="模块",
                    )
                    ev_search = gr.Textbox(placeholder="关键词过滤...", label="搜索")
                ev_table = gr.Dataframe(
                    headers=["Seq", "时间", "级别", "模块", "事件", "数据摘要"],
                    datatype=["str", "str", "str", "str", "str", "str"],
                    interactive=False,
                    wrap=True,
                )

            # ================================================================
            # Tab 3: Radar Scans, Inventory & Task Boards
            # ================================================================
            with gr.Tab("扫描雷达 · 背包 · 任务板"):
                with gr.Row():
                    radar_kind = gr.Radio(choices=["poi", "census", "terrain"], value="poi", label="雷达类型", scale=2)
                    radar_radius = gr.Slider(4, 48, value=24, step=2, label="半径", scale=2)
                    btn_radar = gr.Button("执行主动雷达扫描", variant="primary", size="sm", scale=1)
                    radar_msg = gr.Textbox(label="扫描状态", interactive=False, scale=3)
                with gr.Row():
                    radar_json = gr.Code(label="雷达结果 JSON", language="json", value="{}", lines=9)
                    inv_table = gr.Dataframe(
                        headers=["物品名称", "数量", "耐久/备注"],
                        datatype=["str", "str", "str"],
                        label="实时背包统计",
                        interactive=False,
                    )
                with gr.Row():
                    entities_table = gr.Dataframe(
                        headers=["ID", "名称", "类型", "坐标", "距离"],
                        datatype=["str", "str", "str", "str", "str"],
                        label="附近实体",
                        interactive=False,
                    )
                    blocks_table = gr.Dataframe(
                        headers=["方块名称", "坐标", "距离", "可见/可挖"],
                        datatype=["str", "str", "str", "str"],
                        label="观测候选方块",
                        interactive=False,
                    )
                with gr.Row():
                    board_daily = gr.Code(label="日常任务板 & Playbook", language="json", value="{}", lines=9)
                    board_comp = gr.Code(label="赛事得分板 (Competition)", language="json", value="{}", lines=9)

            # ================================================================
            # Tab 4: Runtime Parameter Tuning & config.yaml Persistence
            # ================================================================
            with gr.Tab("参数调优与 config.yaml"):
                gr.HTML("<div class='flat-section-title'>校验后热生效并写回 config.yaml（密码/Key 留空保持原值）</div>")
                with gr.Accordion("1. 基础与运行模式", open=True):
                    with gr.Row():
                        cfg_mode = gr.Dropdown(["sleep", "survival"], value=init_cfg["agent"]["mode"], label="agent.mode")
                        cfg_objective = gr.Dropdown(["starter", "daily", "competition"], value=init_cfg["survival"]["objective"], label="survival.objective")
                        cfg_intent = gr.Dropdown(["survival", "speedrun"], value=init_cfg["survival"]["intent"], label="survival.intent")
                        cfg_sleep_policy = gr.Dropdown(["opportunistic", "strict"], value=init_cfg["survival"]["sleep_policy"], label="survival.sleep_policy")
                    with gr.Row():
                        cfg_username = gr.Textbox(value=init_cfg["agent"]["name"], label="agent.name")
                        cfg_mc_url = gr.Textbox(value=init_cfg["mc_api"]["base_url"], label="mc_api.base_url")
                        cfg_world_id = gr.Textbox(value=init_cfg["survival"]["world_id"], label="survival.world_id")
                        cfg_keep_conn = gr.Checkbox(value=init_cfg["mc_api"]["keep_session_on_exit"], label="keep_session_on_exit")
                    cfg_op_goal = gr.Textbox(value=init_cfg["survival"]["operator_goal"], label="survival.operator_goal", lines=2)

                with gr.Accordion("2. 生存阈值与轮询预算", open=True):
                    with gr.Row():
                        cfg_poll = gr.Number(value=init_cfg["survival"]["poll_interval_seconds"], label="poll_interval_seconds")
                        cfg_scan_r = gr.Number(value=init_cfg["survival"]["scan_radius"], label="scan_radius")
                        cfg_low_hp = gr.Number(value=init_cfg["survival"]["low_health_threshold"], label="low_health")
                        cfg_food_th = gr.Number(value=init_cfg["survival"]["food_threshold"], label="food_threshold")
                        cfg_threat_r = gr.Number(value=init_cfg["survival"]["threat_radius"], label="threat_radius")
                    with gr.Row():
                        cfg_exp_dist = gr.Number(value=init_cfg["survival"]["explore_distance"], label="explore_distance")
                        cfg_exp_steps = gr.Number(value=init_cfg["survival"]["max_explore_steps"], label="max_explore_steps")
                        cfg_max_steps = gr.Number(value=init_cfg["survival"]["max_steps"], label="max_steps")
                        cfg_max_rt = gr.Number(value=init_cfg["survival"]["max_runtime_seconds"], label="max_runtime_s")
                        cfg_max_fail = gr.Number(value=init_cfg["survival"]["max_consecutive_failures"], label="max_failures")
                        cfg_max_deaths = gr.Number(value=init_cfg["survival"]["max_deaths"], label="max_deaths")

                with gr.Accordion("3. Autonomy 长任务与 Daily 储备", open=False):
                    with gr.Row():
                        cfg_auto_en = gr.Checkbox(value=init_cfg["survival"]["autonomy"]["enabled"], label="autonomy.enabled")
                        cfg_auto_steps = gr.Number(value=init_cfg["survival"]["autonomy"]["max_steps"], label="autonomy.max_steps")
                        cfg_auto_secs = gr.Number(value=init_cfg["survival"]["autonomy"]["max_seconds"], label="autonomy.max_s")
                        cfg_auto_fail = gr.Number(value=init_cfg["survival"]["autonomy"]["max_local_failures"], label="local_failures")
                        cfg_auto_noprog = gr.Number(value=init_cfg["survival"]["autonomy"]["max_no_progress_steps"], label="no_progress")
                        cfg_auto_exp = gr.Number(value=init_cfg["survival"]["autonomy"]["max_explore_steps"], label="auto_explore")
                    with gr.Row():
                        cfg_daily_food = gr.Number(value=init_cfg["survival"]["daily"]["food_reserve"], label="food_reserve")
                        cfg_daily_wood = gr.Number(value=init_cfg["survival"]["daily"]["wood_reserve"], label="wood_reserve")
                        cfg_daily_fuel = gr.Number(value=init_cfg["survival"]["daily"]["fuel_reserve"], label="fuel_reserve")

                with gr.Accordion("4. 大模型参数 (LLM)", open=False):
                    with gr.Row():
                        cfg_llm_en = gr.Checkbox(value=init_cfg["llm"]["enabled"], label="llm.enabled")
                        cfg_llm_url = gr.Textbox(value=init_cfg["llm"]["base_url"], label="llm.base_url")
                        cfg_llm_model = gr.Textbox(value=init_cfg["llm"]["model"], label="llm.model")
                        cfg_llm_key = gr.Textbox(value="", type="password", label="新 api_key (留空不变)", placeholder="已配置" if init_cfg["llm"]["has_api_key"] else "未设置")
                    with gr.Row():
                        cfg_llm_temp = gr.Slider(0.0, 2.0, value=init_cfg["llm"]["temperature"], step=0.05, label="temperature")
                        cfg_llm_tokens = gr.Number(value=init_cfg["llm"]["max_tokens"], label="max_tokens")
                        cfg_llm_to = gr.Number(value=init_cfg["llm"]["timeout_seconds"], label="timeout_s")
                        cfg_llm_hist = gr.Number(value=init_cfg["llm"]["max_history_turns"], label="max_history")
                        cfg_llm_rep = gr.Number(value=init_cfg["llm"]["max_repair_attempts"], label="max_repair")

                with gr.Accordion("5. 深矿与睡眠策略 (mining / sleep)", open=False):
                    with gr.Row():
                        cfg_mine_flood = gr.Number(value=init_cfg["mining"]["cave_policy"]["flood_max_cells"], label="flood_max_cells")
                        cfg_mine_hr = gr.Number(value=init_cfg["mining"]["cave_policy"]["horizontal_radius"], label="horiz_radius")
                        cfg_mine_er = gr.Number(value=init_cfg["mining"]["cave_policy"]["explore_radius"], label="explore_radius")
                        cfg_mine_to = gr.Number(value=init_cfg["mining"]["cave_policy"]["explore_timeout_seconds"], label="explore_timeout_s")
                        cfg_mine_torch = gr.Number(value=init_cfg["mining"]["cave_policy"]["torch_interval"], label="torch_interval")
                        cfg_mine_vein = gr.Number(value=init_cfg["mining"]["cave_policy"]["max_vein_blocks"], label="max_vein_blocks")
                    with gr.Row():
                        cfg_home_slots = gr.Number(value=init_cfg["mining"]["home_thresholds"]["min_empty_slots"], label="min_empty_slots")
                        cfg_home_hp = gr.Number(value=init_cfg["mining"]["home_thresholds"]["min_health"], label="min_health")
                        cfg_home_food = gr.Number(value=init_cfg["mining"]["home_thresholds"]["min_food"], label="min_food")
                        cfg_home_torches = gr.Number(value=init_cfg["mining"]["home_thresholds"]["min_torches"], label="min_torches")
                        cfg_sleep_rad = gr.Number(value=init_cfg["sleep"]["radius"], label="sleep.radius")
                        cfg_sleep_threat = gr.Number(value=init_cfg["sleep"]["threat_distance"], label="sleep.threat_dist")

                with gr.Row():
                    btn_save_cfg = gr.Button("应用热更新并保存 config.yaml", variant="primary", size="sm")
                    btn_reload_cfg = gr.Button("刷新表单", size="sm")
                    btn_restart_from_cfg = gr.Button("重启 Runner", size="sm")
                    cfg_feedback = gr.Textbox(placeholder="保存状态...", show_label=False, interactive=False, scale=3)

        # --- Event Handlers ---

        def on_pick_voxel(evt: gr.EventData):
            data = getattr(evt, "_data", None)
            if not isinstance(data, dict):
                data = {}
            x = data.get("x")
            y = data.get("y")
            z = data.get("z")
            name = data.get("name", "block")
            if x is None or y is None or z is None:
                return gr.update(), gr.update(), gr.update(), gr.update()
            return x, y, z, f"选中 {name} -> [{x}, {y}, {z}]"

        viewport_3d.click(on_pick_voxel, outputs=[target_x, target_y, target_z, ctrl_feedback])

        btn_join_game.click(
            lambda uname, md, obj: runtime.connect_or_restart(start_paused=False, username=uname, mode=md, objective=obj)[1],
            inputs=[launch_username, launch_mode, launch_objective],
            outputs=[ctrl_feedback],
        )
        btn_join_paused.click(
            lambda uname, md, obj: runtime.connect_or_restart(start_paused=True, username=uname, mode=md, objective=obj)[1],
            inputs=[launch_username, launch_mode, launch_objective],
            outputs=[ctrl_feedback],
        )
        btn_disconnect.click(lambda: runtime.disconnect_from_game()[1], outputs=[ctrl_feedback])
        btn_restart_from_cfg.click(lambda: runtime.connect_or_restart()[1], outputs=[cfg_feedback])
        btn_pause.click(lambda: runtime.pause_ai()[1], outputs=[ctrl_feedback])
        btn_resume.click(lambda: runtime.resume_ai()[1], outputs=[ctrl_feedback])
        btn_step.click(lambda: runtime.step_once()[1], outputs=[ctrl_feedback])
        btn_estop.click(lambda: runtime.emergency_stop()[1], outputs=[ctrl_feedback])
        btn_ack_llm.click(lambda: runtime.acknowledge_model_idle()[1], outputs=[ctrl_feedback])

        scan_btn.click(
            lambda rx, ry: runtime.manual_3d_scan(radius_xz=int(rx), radius_y=int(ry))[1],
            inputs=[scan_rx, scan_ry],
            outputs=[scan_result],
        )

        def _move(direction):
            return lambda ms, sp, sn, fo: runtime.manual_move_step(
                direction, duration_ms=int(ms), sprint=bool(sp), sneak=bool(sn), force_override=bool(fo)
            )[1]

        for btn, direction in (
            (btn_fwd, "forward"),
            (btn_back, "back"),
            (btn_left, "left"),
            (btn_right, "right"),
            (btn_jump, "jump"),
        ):
            btn.click(_move(direction), inputs=[move_ms, move_sprint, move_sneak, force_override], outputs=[ctrl_feedback])

        btn_moveto.click(
            lambda x, y, z, r, fo: runtime.manual_move_to(float(x), float(y), float(z), range_blocks=float(r), force_override=bool(fo))[1],
            inputs=[target_x, target_y, target_z, target_range, force_override],
            outputs=[ctrl_feedback],
        )
        btn_lookat.click(
            lambda x, y, z, fo: runtime.manual_look_at(float(x), float(y), float(z), force_override=bool(fo))[1],
            inputs=[target_x, target_y, target_z, force_override],
            outputs=[ctrl_feedback],
        )
        btn_run_skill.click(
            lambda name, args, fo: runtime.manual_run_skill(name, args, force_override=bool(fo))[1],
            inputs=[skill_dd, skill_args, force_override],
            outputs=[ctrl_feedback],
        )

        def on_send_chat(msg: str):
            ok, feedback = runtime.send_chat(msg)
            chat_txt = _format_chat_log(runtime.status_report().get("chat", []))
            return ("" if ok else msg), chat_txt, feedback

        btn_send_chat.click(on_send_chat, inputs=[chat_input], outputs=[chat_input, chat_log, ctrl_feedback])
        chat_input.submit(on_send_chat, inputs=[chat_input], outputs=[chat_input, chat_log, ctrl_feedback])

        def on_radar(kind: str, radius: int):
            _, data, msg = runtime.manual_radar_scan(kind=kind, radius=int(radius))
            return json.dumps(data, ensure_ascii=False, indent=2), msg

        btn_radar.click(on_radar, inputs=[radar_kind, radar_radius], outputs=[radar_json, radar_msg])

        def on_save_config(
            mode, objective, intent, sleep_policy, username, mc_url, world_id, keep_conn, op_goal,
            poll, scan_r, low_hp, food_th, threat_r, exp_dist, exp_steps, max_steps, max_rt, max_fail, max_deaths,
            auto_en, auto_steps, auto_secs, auto_fail, auto_noprog, auto_exp,
            daily_food, daily_wood, daily_fuel,
            llm_en, llm_url, llm_model, llm_key, llm_temp, llm_tokens, llm_to, llm_hist, llm_rep,
            mine_flood, mine_hr, mine_er, mine_to, mine_torch, mine_vein,
            home_slots, home_hp, home_food, home_torches, sleep_rad, sleep_threat,
        ):
            updates = {
                "mc_api": {"base_url": str(mc_url).strip(), "keep_session_on_exit": bool(keep_conn)},
                "agent": {"name": str(username).strip(), "mode": str(mode)},
                "survival": {
                    "objective": str(objective),
                    "intent": str(intent),
                    "sleep_policy": str(sleep_policy),
                    "operator_goal": str(op_goal or ""),
                    "world_id": str(world_id).strip(),
                    "poll_interval_seconds": float(poll),
                    "scan_radius": int(scan_r),
                    "low_health_threshold": float(low_hp),
                    "food_threshold": float(food_th),
                    "threat_radius": float(threat_r),
                    "explore_distance": float(exp_dist),
                    "max_explore_steps": int(exp_steps),
                    "max_steps": int(max_steps),
                    "max_runtime_seconds": float(max_rt),
                    "max_consecutive_failures": int(max_fail),
                    "max_deaths": int(max_deaths),
                    "autonomy": {
                        "enabled": bool(auto_en),
                        "max_steps": int(auto_steps),
                        "max_seconds": float(auto_secs),
                        "max_local_failures": int(auto_fail),
                        "max_no_progress_steps": int(auto_noprog),
                        "max_explore_steps": int(auto_exp),
                    },
                    "daily": {
                        "food_reserve": int(daily_food),
                        "wood_reserve": int(daily_wood),
                        "fuel_reserve": int(daily_fuel),
                    },
                },
                "llm": {
                    "enabled": bool(llm_en),
                    "base_url": str(llm_url or "").strip(),
                    "model": str(llm_model or "").strip(),
                    "api_key": str(llm_key or ""),
                    "temperature": float(llm_temp),
                    "max_tokens": int(llm_tokens),
                    "timeout_seconds": float(llm_to),
                    "max_history_turns": int(llm_hist),
                    "max_repair_attempts": int(llm_rep),
                },
                "mining": {
                    "cave_policy": {
                        "flood_max_cells": int(mine_flood),
                        "horizontal_radius": int(mine_hr),
                        "explore_radius": int(mine_er),
                        "explore_timeout_seconds": float(mine_to),
                        "torch_interval": int(mine_torch),
                        "max_vein_blocks": int(mine_vein),
                    },
                    "home_thresholds": {
                        "min_empty_slots": int(home_slots),
                        "min_health": float(home_hp),
                        "min_food": float(home_food),
                        "min_torches": int(home_torches),
                    },
                },
                "sleep": {
                    "radius": int(sleep_rad),
                    "threat_distance": float(sleep_threat),
                },
            }
            _, msg = runtime.update_and_save_settings(updates)
            return msg

        cfg_inputs = [
            cfg_mode, cfg_objective, cfg_intent, cfg_sleep_policy, cfg_username, cfg_mc_url, cfg_world_id, cfg_keep_conn, cfg_op_goal,
            cfg_poll, cfg_scan_r, cfg_low_hp, cfg_food_th, cfg_threat_r, cfg_exp_dist, cfg_exp_steps, cfg_max_steps, cfg_max_rt, cfg_max_fail, cfg_max_deaths,
            cfg_auto_en, cfg_auto_steps, cfg_auto_secs, cfg_auto_fail, cfg_auto_noprog, cfg_auto_exp,
            cfg_daily_food, cfg_daily_wood, cfg_daily_fuel,
            cfg_llm_en, cfg_llm_url, cfg_llm_model, cfg_llm_key, cfg_llm_temp, cfg_llm_tokens, cfg_llm_to, cfg_llm_hist, cfg_llm_rep,
            cfg_mine_flood, cfg_mine_hr, cfg_mine_er, cfg_mine_to, cfg_mine_torch, cfg_mine_vein,
            cfg_home_slots, cfg_home_hp, cfg_home_food, cfg_home_torches, cfg_sleep_rad, cfg_sleep_threat,
        ]
        btn_save_cfg.click(on_save_config, inputs=cfg_inputs, outputs=[cfg_feedback])

        def on_reload_form():
            c = runtime.get_editable_settings()
            return [
                c["agent"]["mode"], c["survival"]["objective"], c["survival"]["intent"], c["survival"]["sleep_policy"],
                c["agent"]["name"], c["mc_api"]["base_url"], c["survival"]["world_id"], c["mc_api"]["keep_session_on_exit"],
                c["survival"]["operator_goal"],
                c["survival"]["poll_interval_seconds"], c["survival"]["scan_radius"], c["survival"]["low_health_threshold"],
                c["survival"]["food_threshold"], c["survival"]["threat_radius"], c["survival"]["explore_distance"],
                c["survival"]["max_explore_steps"], c["survival"]["max_steps"], c["survival"]["max_runtime_seconds"],
                c["survival"]["max_consecutive_failures"], c["survival"]["max_deaths"],
                c["survival"]["autonomy"]["enabled"], c["survival"]["autonomy"]["max_steps"],
                c["survival"]["autonomy"]["max_seconds"], c["survival"]["autonomy"]["max_local_failures"],
                c["survival"]["autonomy"]["max_no_progress_steps"], c["survival"]["autonomy"]["max_explore_steps"],
                c["survival"]["daily"]["food_reserve"], c["survival"]["daily"]["wood_reserve"], c["survival"]["daily"]["fuel_reserve"],
                c["llm"]["enabled"], c["llm"]["base_url"], c["llm"]["model"], "",
                c["llm"]["temperature"], c["llm"]["max_tokens"], c["llm"]["timeout_seconds"],
                c["llm"]["max_history_turns"], c["llm"]["max_repair_attempts"],
                c["mining"]["cave_policy"]["flood_max_cells"], c["mining"]["cave_policy"]["horizontal_radius"],
                c["mining"]["cave_policy"]["explore_radius"], c["mining"]["cave_policy"]["explore_timeout_seconds"],
                c["mining"]["cave_policy"]["torch_interval"], c["mining"]["cave_policy"]["max_vein_blocks"],
                c["mining"]["home_thresholds"]["min_empty_slots"], c["mining"]["home_thresholds"]["min_health"],
                c["mining"]["home_thresholds"]["min_food"], c["mining"]["home_thresholds"]["min_torches"],
                c["sleep"]["radius"], c["sleep"]["threat_distance"],
                "已从当前配置刷新表单",
            ]

        btn_reload_cfg.click(on_reload_form, outputs=[*cfg_inputs, cfg_feedback])

        # --- Periodic telemetry refresh ---

        def on_refresh(lvl: str, mod: str, kw: str):
            report = runtime.status_report()
            banner = _format_status_banner(report)
            chat_txt = _format_chat_log(report.get("chat", []))

            events = runtime.get_events(level=lvl, module=mod, search=kw, limit=80)
            card_a, card_b, card_c = _format_live_cockpit_cards(report, events)

            boards = report.get("boards", {})
            rt = report.get("runtime_code", {})
            stack_txt, task_txt, res_txt = _format_runtime_code(rt, boards)

            llm = (report.get("monitor") or {}).get("llm") or {}
            req_txt = json.dumps(llm.get("request") or {}, ensure_ascii=False, indent=2)
            resp_obj = llm.get("error") or llm.get("response") or {}
            resp_txt = json.dumps(resp_obj, ensure_ascii=False, indent=2)

            ev_rows = _format_events_rows(events)

            obs = report.get("observation") or {}
            inv_rows = [
                [
                    str(item.get("name", "")),
                    str(item.get("count", 1)),
                    f"dur={item.get('durability')}" if item.get("durability") is not None else "",
                ]
                for item in (obs.get("inventory") or [])[:60]
                if isinstance(item, dict)
            ]
            ent_rows = [
                [
                    str(e.get("id", "")),
                    str(e.get("name", "")),
                    str(e.get("type", "")),
                    str(e.get("pos", "")),
                    str(e.get("dist", "")),
                ]
                for e in (obs.get("entities") or [])[:40]
                if isinstance(e, dict)
            ]
            blk_rows = [
                [
                    str(b.get("name", "")),
                    str(b.get("pos", "")),
                    str(b.get("dist", "")),
                    f"see={b.get('can_see')} dig={b.get('can_dig')}",
                ]
                for b in (obs.get("blocks") or [])[:60]
                if isinstance(b, dict)
            ]

            daily_obj = {
                "daily_tasks": boards.get("daily_tasks"),
                "playbook": boards.get("playbook"),
                "memory_summary": boards.get("memory_summary"),
            }
            daily_txt = json.dumps(daily_obj, ensure_ascii=False, indent=2)
            comp_txt = json.dumps(boards.get("competition") or {}, ensure_ascii=False, indent=2)

            return (
                banner, card_a, card_b, card_c, chat_txt,
                stack_txt, task_txt, res_txt, req_txt, resp_txt, ev_rows,
                inv_rows, ent_rows, blk_rows, daily_txt, comp_txt,
            )

        timer = gr.Timer(1.2)
        timer.tick(
            on_refresh,
            inputs=[ev_level, ev_module, ev_search],
            outputs=[
                banner_html, live_card_code, live_card_state, live_card_events, chat_log,
                stack_md, task_exec_code, last_res_code, llm_req_code, llm_resp_code, ev_table,
                inv_table, entities_table, blocks_table, board_daily, board_comp,
            ],
        )

    return demo


def _ensure_loopback_no_proxy() -> None:
    """Ensure Gradio's local startup probe bypasses any configured HTTP(S) proxy."""
    for env_key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(env_key, "")
        parts = [item.strip() for item in existing.split(",") if item.strip()]
        changed = False
        for host in ("127.0.0.1", "localhost"):
            if host not in parts:
                parts.append(host)
                changed = True
        if changed or not existing:
            os.environ[env_key] = ",".join(parts)


def launch_web_console(
    *,
    config_path: str | Path = "config.yaml",
    username: Optional[str] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
    host: str = "127.0.0.1",
    port: int = 18940,
    start_paused: bool = False,
    auto_connect: bool = True,
    resume: bool = False,
    acknowledge_model_idle: bool = False,
    inbrowser: bool = False,
) -> int:
    """Start WebConsoleRuntime and launch the Gradio 6 server with default flat compact UI."""
    if host in {"127.0.0.1", "localhost", "::1"}:
        _ensure_loopback_no_proxy()
    runtime = WebConsoleRuntime(
        config_path=config_path,
        username=username,
        config_overrides=config_overrides,
    )
    try:
        runtime.start(
            auto_connect=auto_connect,
            start_paused=start_paused,
            resume=resume,
            acknowledge_model_idle=acknowledge_model_idle,
        )
        demo = build_demo(runtime)
        server_port = int(port) if port and int(port) > 0 else None
        print(f"BotSleep Gradio Web Console: http://{host}:{server_port or 'auto'}")
        demo.launch(
            server_name=host,
            server_port=server_port,
            inbrowser=inbrowser,
            css=COMPACT_FLAT_CSS,
            prevent_thread_lock=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BotSleep Gradio 3D & AI Developer Workbench")
    parser.add_argument("--config", default=os.environ.get("BOTSLEEP_CONFIG", "config.yaml"))
    parser.add_argument("--username", default=None, help="Override configured Minecraft username")
    parser.add_argument("--host", default="127.0.0.1", help="Gradio server bind host")
    parser.add_argument("--port", type=int, default=18940, help="Gradio server port")
    parser.add_argument("--paused", action="store_true", help="Connect in paused state for manual inspection")
    parser.add_argument("--no-connect", action="store_true", help="Launch UI without auto-connecting to mc-agent-api")
    parser.add_argument("--inbrowser", action="store_true", help="Open browser automatically on launch")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return launch_web_console(
            config_path=args.config,
            username=args.username,
            host=args.host,
            port=args.port,
            start_paused=args.paused,
            auto_connect=not args.no_connect,
            inbrowser=args.inbrowser,
        )
    except (ConfigError, OSError) as exc:
        print(f"Web console error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
