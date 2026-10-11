import { useEffect, useRef } from "react";
import * as THREE from "three";
import { loadArtworkTexture, loadSceneModel } from "../lib/sceneAssets";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

export type CarouselSleeve = { index: number; offset: number; artworkUrl?: string | null };
type Props = {
  sleeves: CarouselSleeve[];
  onReady: (ready: boolean) => void;
  onSettled?: () => void;
  motionSpeed?: number;
};
type SleevePose = { position: THREE.Vector3; rotation: THREE.Euler; scale: number };
type RecordSleeve = {
  model: THREE.Group;
  artwork: THREE.Mesh;
  material: THREE.MeshStandardMaterial;
  url?: string | null;
  request: number;
  loaded: boolean;
  offset: number;
  target: number;
  retiring: boolean;
  fromOffset: number;
  fromPose: SleevePose | null;
  elapsed: number;
  gesture: "inspect" | "return" | "shelf";
};

// Separating-axis test for the entire jacket, including both panels and edges.
// When volumes overlap, find the smallest forward translation that separates them.
function forwardClearance(a: THREE.Group, b: THREE.Group): number {
  const axesA = [
    new THREE.Vector3(1, 0, 0),
    new THREE.Vector3(0, 1, 0),
    new THREE.Vector3(0, 0, 1),
  ].map((v) => v.applyEuler(a.rotation));
  const axesB = [
    new THREE.Vector3(1, 0, 0),
    new THREE.Vector3(0, 1, 0),
    new THREE.Vector3(0, 0, 1),
  ].map((v) => v.applyEuler(b.rotation));
  const axes = [...axesA, ...axesB];
  for (const x of axesA)
    for (const y of axesB) {
      const cross = x.clone().cross(y);
      if (cross.lengthSq() > 1e-10) axes.push(cross.normalize());
    }
  const delta = a.position.clone().sub(b.position);
  const half = [1, 1, 0.04];
  let exit = Infinity;
  for (const axis of axes) {
    const radius =
      half.reduce(
        (sum, size, i) =>
          sum +
          size *
            (a.scale.x * Math.abs(axis.dot(axesA[i])) + b.scale.x * Math.abs(axis.dot(axesB[i]))),
        0,
      ) + 2;
    const distance = delta.dot(axis);
    if (Math.abs(distance) >= radius) return 0;
    if (Math.abs(axis.z) > 1e-8)
      exit = Math.min(exit, (radius - Math.sign(axis.z) * distance) / Math.abs(axis.z));
  }
  return exit + 0.01;
}

/** One decorative canvas; the browser's DOM owns selection, focus and gestures. */
export default function AlbumCarouselScene({
  sleeves,
  onReady,
  onSettled,
  motionSpeed = 1,
}: Props) {
  const playbackRef = useRef({ onSettled, motionSpeed });
  playbackRef.current = { onSettled, motionSpeed };
  const hostRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef(sleeves);
  const updateRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    stateRef.current = sleeves;
    updateRef.current?.();
  }, [sleeves]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    onReady(false);
    let disposed = false;
    let lost = false;
    let frame = 0;
    let previous = 0;
    let navigating = false;
    let template: THREE.Group | undefined;
    let renderer: THREE.WebGLRenderer;
    const records = new Map<number, RecordSleeve>();
    const geometries = new Set<THREE.BufferGeometry>();
    const materials = new Set<THREE.Material>();
    const textures = new Set<THREE.Texture>();
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-200, 200, 120, -120, 0.1, 2500);
    camera.position.set(0, 0, 1200);
    try {
      renderer = new THREE.WebGLRenderer({
        alpha: true,
        antialias: true,
        powerPreference: "low-power",
      });
    } catch {
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1;
    host.appendChild(renderer.domElement);
    const environmentScene = new RoomEnvironment();
    const generator = new THREE.PMREMGenerator(renderer);
    const environment = generator.fromScene(environmentScene, 0.04);
    environmentScene.dispose();
    generator.dispose();
    scene.environment = environment.texture;
    scene.environmentIntensity = 0.32;
    scene.add(new THREE.HemisphereLight(0xfff1e1, 0x20221d, 0.65));
    const key = new THREE.DirectionalLight(0xfff0df, 2.2);
    key.position.set(-350, 420, 600);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xffffff, 1.2);
    rim.position.set(450, 180, 300);
    scene.add(rim);
    let width = 0;
    let height = 0;
    let albumSize = 150;
    let rem = 16;
    let mobile = false;
    let stageOriginY = 0;
    let ready = false;
    function notifyReady(value: boolean) {
      if (ready === value) return;
      ready = value;
      onReady(value);
    }
    function resize() {
      const nextWidth = host!.clientWidth;
      const nextHeight = host!.clientHeight;
      const stage = host!.parentElement!;
      stageOriginY = -host!.offsetTop;
      const item = stage.querySelector<HTMLElement>(".album-guess-item");
      // Read the inherited size variable rather than a moving projected hit area.
      const probe = stage.querySelector<HTMLElement>(".album-guess-size-probe");
      albumSize = probe?.getBoundingClientRect().width || item?.offsetWidth || albumSize;
      rem = Number.parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      mobile = stage.clientWidth < 340;
      if (!nextWidth || !nextHeight) return;
      if (width !== nextWidth || height !== nextHeight) {
        width = nextWidth;
        height = nextHeight;
        renderer.setSize(width, height, false);
        camera.left = -width / 2;
        camera.right = width / 2;
        camera.top = height / 2;
        camera.bottom = -height / 2;
        camera.updateProjectionMatrix();
      }
    }
    function removeRecord(index: number, record: RecordSleeve) {
      scene.remove(record.model);
      record.material.map?.dispose();
      record.material.dispose();
      records.delete(index);
    }
    function loadArtwork(record: RecordSleeve, url?: string | null) {
      record.url = url;
      record.loaded = !url;
      const request = ++record.request;
      if (!url) {
        record.material.map?.dispose();
        record.material.map = null;
        record.material.color.set(0xd35e43);
        record.material.needsUpdate = true;
        return;
      }
      void loadArtworkTexture(url)
        .then((texture) => {
          if (disposed || request !== record.request || ![...records.values()].includes(record)) {
            texture.dispose();
            return;
          }
          texture.flipY = false;
          texture.colorSpace = THREE.SRGBColorSpace;
          texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 4);
          record.material.map?.dispose();
          record.material.map = texture;
          record.material.color.set(0xffffff);
          record.material.needsUpdate = true;
          record.loaded = true;
          wake();
        })
        .catch(() => {
          if (!disposed && request === record.request) wake();
        });
    }
    function beginGesture(record: RecordSleeve, target: number) {
      if (record.target === target) return;
      navigating = true;
      record.fromOffset = record.offset;
      record.fromPose = {
        position: record.model.position.clone(),
        rotation: record.model.rotation.clone(),
        scale: record.model.scale.x,
      };
      record.elapsed = 0;
      record.gesture =
        target === 0 ? "inspect" : Math.abs(record.offset) < 0.95 ? "return" : "shelf";
      record.target = target;
    }
    function synchronize() {
      if (!template || disposed) return;
      const current = new Set(stateRef.current.map(({ index }) => index));
      for (const [index, record] of records) {
        if (!current.has(index)) {
          record.retiring = true;
          beginGesture(record, Math.sign(record.offset || 1) * 7);
        }
      }
      for (const sleeve of stateRef.current) {
        let record = records.get(sleeve.index);
        if (!record) {
          const model = template.clone(true);
          const artwork = model.getObjectByName("SleeveArtwork");
          if (!(artwork instanceof THREE.Mesh)) continue;
          const material = new THREE.MeshStandardMaterial({
            roughness: 0.82,
            metalness: 0,
            color: 0xd35e43,
          });
          artwork.material = material;
          record = {
            model,
            artwork,
            material,
            request: 0,
            loaded: false,
            offset: sleeve.offset,
            target: sleeve.offset,
            retiring: false,
            fromOffset: sleeve.offset,
            fromPose: null,
            elapsed: 0,
            gesture: "shelf",
          };
          records.set(sleeve.index, record);
          scene.add(model);
          loadArtwork(record, sleeve.artworkUrl);
        } else if (record.url !== sleeve.artworkUrl) loadArtwork(record, sleeve.artworkUrl);
        // A circular catalog recycles an end jacket at the opposite shelf edge.
        // Never sweep that recycled jacket through the selected cover.
        if (
          record.offset * sleeve.offset < 0 &&
          Math.abs(record.offset - sleeve.offset) > Math.max(1, stateRef.current.length / 2)
        ) {
          record.offset = sleeve.offset;
          record.target = sleeve.offset;
          record.fromPose = null;
        }
        beginGesture(record, sleeve.offset);
        record.retiring = false;
      }
      wake();
    }
    function render(now: number) {
      frame = 0;
      if (disposed || lost || !template || document.hidden) return;
      resize();
      const dt = previous ? Math.min((now - previous) / 1000, 0.05) : 1 / 60;
      previous = now;
      const smooth = (t: number) => {
        const v = Math.max(0, Math.min(t, 1));
        return v * v * (3 - 2 * v);
      };
      const mix = (a: number, b: number, t: number) => a + (b - a) * t;
      let moving = false;
      for (const [index, record] of records) {
        record.elapsed += dt * playbackRef.current.motionSpeed;
        const inspecting = record.gesture === "inspect";
        const returning = record.gesture === "return";
        const duration = inspecting ? 0.55 : returning ? 0.39 : 0.45;
        const delay = inspecting ? 0.425 : 0;
        const t =
          reducedMotion.matches || !record.fromPose
            ? 1
            : Math.max(0, Math.min((record.elapsed - delay) / duration, 1));
        record.offset = mix(record.fromOffset, record.target, smooth(t));
        if (t < 1) moving = true;
        const depth = Math.abs(record.target);
        const side = Math.sign(record.target);
        // Fold back onto the shelf while lifting the chosen jacket toward the viewer.
        const folded = Math.min(depth, 1);
        const activeScale = mobile ? 1.14 : 1.2;
        const scale = activeScale + (0.86 - activeScale) * folded;
        const x =
          side *
          ((mobile ? 3.7 : 4.8) * rem * folded +
            Math.max(depth - 1, 0) * (mobile ? 0.86 : 2.1) * rem);
        const lift = (mobile ? 0.55 : 1.25) * rem;
        const y =
          height / 2 -
          stageOriginY -
          albumSize / 2 -
          0.5 * rem +
          lift * (1 - folded) -
          0.45 * rem * folded;
        const targetZ = 110 * (1 - folded) - depth * 160;
        const targetXAngle = 0.045 * (1 - folded);
        const targetYAngle = folded * (mobile ? 1.32 : 1.19) - 0.12 * (1 - folded);
        const from = record.fromPose;
        if (from && t < 1) {
          // Lift clear first, pull forward, then turn the cover toward the viewer.
          // Return reverses that physical sequence instead of flattening every axis together.
          const travel = inspecting ? smooth(t / 0.35) : smooth(t);
          const turn = inspecting
            ? smooth((t - 0.45) / 0.35)
            : returning
              ? smooth(t / 0.45)
              : smooth(t);
          const liftPhase = inspecting ? smooth(t / 0.3) : smooth(t);
          const grip = Math.sin(Math.PI * t);
          const direction = Math.sign(record.fromOffset || record.target || 1);
          const inspectSettle = inspecting && t > 0.7 ? Math.sin((Math.PI * (t - 0.7)) / 0.3) : 0;
          record.model.position.set(
            mix(from.position.x, x, travel),
            mix(from.position.y, y, liftPhase) +
              grip * rem * (inspecting ? 0.5 : returning ? 0.18 : 0),
            mix(from.position.z, targetZ, inspecting ? smooth(t / 0.35) : smooth((t - 0.5) / 0.5)) +
              (inspecting ? grip * 22 : 0),
          );
          record.model.rotation.set(
            mix(from.rotation.x, targetXAngle, smooth(t)) + (inspecting ? grip * 0.07 : 0),
            mix(from.rotation.y, targetYAngle, turn) - inspectSettle * 0.12,
            mix(from.rotation.z, 0, turn) -
              direction * grip * (inspecting ? 0.045 : returning ? 0.025 : 0),
          );
          record.model.scale.setScalar(
            mix(from.scale, (albumSize * scale) / 2, travel) +
              (inspecting ? grip * albumSize * 0.018 : 0),
          );
        } else {
          record.model.position.set(x, y, targetZ);
          record.model.rotation.set(targetXAngle, targetYAngle, 0);
          record.model.scale.setScalar((albumSize * scale) / 2);
          record.fromPose = null;
          record.offset = record.target;
        }
        if (record.retiring && t === 1) {
          removeRecord(index, record);
          continue;
        }
      }
      // Place the shelf first, then returning covers, then the inspected cover.
      // Depth corrections preserve the visible layout while keeping real volumes apart.
      const placed: THREE.Group[] = [];
      const ordered = [...records.entries()].sort(([, a], [, b]) => {
        const rank = (r: RecordSleeve) =>
          r.target === 0 ? -2 : r.fromPose && r.gesture === "return" ? -1 : Math.abs(r.target);
        return rank(b) - rank(a);
      });
      for (const [index, record] of ordered) {
        for (let pass = 0; pass < 32; pass++) {
          let shift = 0;
          for (const other of placed) {
            const clearance = forwardClearance(record.model, other);
            record.model.position.z += clearance;
            shift += clearance;
          }
          if (shift === 0) break;
        }
        placed.push(record.model);
        // Project matching transparent DOM buttons so pointer and focus targets follow the jackets.
        const item = host!.parentElement!.querySelector<HTMLElement>(
          `[data-album-index="${index}"]`,
        );
        if (item) {
          const drawnSize = record.model.scale.x * 2;
          const hitWidth = Math.max(12, drawnSize * Math.cos(record.model.rotation.y));
          item.style.setProperty("--scene-x", `${record.model.position.x}px`);
          item.style.setProperty(
            "--scene-y",
            `${height / 2 - record.model.position.y - stageOriginY - drawnSize / 2}px`,
          );
          item.style.setProperty("--scene-width", `${hitWidth}px`);
          item.style.setProperty("--scene-height", `${drawnSize}px`);
          item.style.zIndex = String(40 - Math.round(Math.abs(record.offset) * 3));
        }
      }
      renderer.render(scene, camera);
      const active = stateRef.current.find(({ offset }) => offset === 0);
      notifyReady(active !== undefined && records.get(active.index)?.loaded === true);
      if (moving && !reducedMotion.matches) frame = requestAnimationFrame(render);
      else if (navigating) {
        navigating = false;
        playbackRef.current.onSettled?.();
      }
    }
    function wake() {
      if (!frame && !disposed && !lost && template && !document.hidden)
        frame = requestAnimationFrame(render);
    }
    function visibilityChanged() {
      cancelAnimationFrame(frame);
      frame = 0;
      previous = 0;
      if (!document.hidden) wake();
    }
    function contextLost(event: Event) {
      event.preventDefault();
      lost = true;
      cancelAnimationFrame(frame);
      frame = 0;
      notifyReady(false);
    }
    renderer.domElement.addEventListener("webglcontextlost", contextLost);
    document.addEventListener("visibilitychange", visibilityChanged);
    reducedMotion.addEventListener("change", wake);
    const observer = new ResizeObserver(() => {
      resize();
      wake();
    });
    observer.observe(host);
    updateRef.current = synchronize;
    function disposeTemplate(model: THREE.Group) {
      model.traverse((object) => {
        if (!(object instanceof THREE.Mesh)) return;
        geometries.add(object.geometry);
        for (const material of Array.isArray(object.material)
          ? object.material
          : [object.material]) {
          materials.add(material);
          for (const value of Object.values(material))
            if (value instanceof THREE.Texture) textures.add(value);
        }
      });
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      for (const texture of textures) texture.dispose();
    }
    void loadSceneModel("/models/songuess/sleeve.glb")
      .then((asset) => {
        if (disposed) {
          disposeTemplate(asset.scene);
          return;
        }
        template = asset.scene;
        resize();
        synchronize();
      })
      .catch(() => {
        if (!disposed) notifyReady(false);
      });
    return () => {
      disposed = true;
      updateRef.current = null;
      cancelAnimationFrame(frame);
      observer.disconnect();
      document.removeEventListener("visibilitychange", visibilityChanged);
      reducedMotion.removeEventListener("change", wake);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      for (const [index, record] of records) removeRecord(index, record);
      if (template) disposeTemplate(template);
      environment.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, [onReady]);
  return <div ref={hostRef} className="album-guess-scene" aria-hidden="true" />;
}
