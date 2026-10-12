import { useEffect, useLayoutEffect, useRef } from "react";
import * as THREE from "three";
import { loadArtworkTexture, loadSceneModel } from "../lib/sceneAssets";
import {
  REVEAL_DURATION,
  UI_FADE_DURATION,
  NEEDLE_PARK_DURATION,
  NEEDLE_FADE_DURATION,
  RETURN_DURATION,
  revealPose,
  returnPose,
  smooth,
  type DeckPose,
} from "../lib/vinylMotion";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

type Props = {
  artworkUrl?: string | null;
  revealed: boolean;
  revealStartedAt: number;
  isPlaying: boolean;
  revealRequested: boolean;
  onReturning: (returning: boolean) => void;
  onRevealComplete: () => void;
  onReady: (ready: boolean, artworkUrl?: string | null) => void;
  onVinylReady: (ready: boolean) => void;
};

/** Decorative scene. Round state, audio and accessible controls stay in the DOM. */
export default function VinylRevealScene({
  artworkUrl,
  revealed,
  revealStartedAt,
  isPlaying,
  revealRequested,
  onReturning,
  onRevealComplete,
  onReady,
  onVinylReady,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({ revealed, revealStartedAt, isPlaying, artworkUrl, revealRequested });
  const layoutUntilRef = useRef(0);
  const wakeRef = useRef<(() => void) | null>(null);
  const artworkRef = useRef<(() => void) | null>(null);
  useLayoutEffect(() => {
    if (stateRef.current.revealed !== revealed) layoutUntilRef.current = performance.now() + 900;
    stateRef.current = { revealed, revealStartedAt, isPlaying, artworkUrl, revealRequested };
    artworkRef.current?.();
    wakeRef.current?.();
  }, [revealed, revealStartedAt, isPlaying, artworkUrl, revealRequested]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false,
      ready = false,
      frame = 0,
      previous = 0;
    let sleeve: THREE.Group | undefined, vinyl: THREE.Group | undefined;
    let renderer: THREE.WebGLRenderer;
    let angularVelocity = 0;
    const reflectionTextures = new Set<THREE.Texture>();
    const textures = new Set<THREE.Texture>();
    const materials = new Set<THREE.Material>();
    const geometries = new Set<THREE.BufferGeometry>();
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-2, 2, 1.5, -1.5, 0.1, 30);
    camera.position.set(0, 0, 8);
    function track(model: THREE.Group) {
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
    }
    function disposeResources() {
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
      for (const texture of textures) texture.dispose();
    }
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
    renderer.setClearColor(0x000000, 0);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.9;
    host.appendChild(renderer.domElement);
    const environmentScene = new RoomEnvironment();
    const generator = new THREE.PMREMGenerator(renderer);
    const environment = generator.fromScene(environmentScene, 0.04);
    scene.environment = environment.texture;
    scene.environmentIntensity = 0.25;
    environmentScene.dispose();
    generator.dispose();
    scene.add(new THREE.HemisphereLight(0xffeee0, 0x292421, 0.45));
    const key = new THREE.DirectionalLight(0xffead3, 1.5);
    key.position.set(-3, 4, 5);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0xffffff, 0.8);
    rim.position.set(4, 1, 2);
    scene.add(rim);
    const assembly = new THREE.Group();
    scene.add(assembly);
    // A small articulated tonearm sits in deck space, independent of the spin.
    const tonearm = new THREE.Group();
    const arm = new THREE.Group();
    tonearm.add(arm);
    scene.add(tonearm);
    const chrome = new THREE.MeshStandardMaterial({
      color: 0xc5c1b8,
      metalness: 0.62,
      roughness: 0.22,
      envMapIntensity: 1.3,
    });
    const graphite = new THREE.MeshStandardMaterial({
      color: 0x252623,
      metalness: 0.45,
      roughness: 0.38,
    });
    const coral = new THREE.MeshStandardMaterial({ color: 0xff6547, roughness: 0.5 });
    function part(
      geometry: THREE.BufferGeometry,
      material: THREE.Material,
      x: number,
      y: number,
      z: number,
      parent = arm,
    ) {
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(x, y, z);
      parent.add(mesh);
      return mesh;
    }
    // Machined collars, a rounded steel tube and a bevelled headshell share
    // the record's studio lighting instead of presenting flat rectangular faces.
    function collar(radius: number, depth: number, z: number, mat = chrome, parent = tonearm) {
      const mesh = part(
        new THREE.CylinderGeometry(radius, radius, depth, 48),
        mat,
        0,
        0,
        z,
        parent,
      );
      mesh.rotation.x = Math.PI / 2;
      return mesh;
    }
    collar(0.108, 0.055, 0, graphite);
    collar(0.084, 0.065, 0.038);
    collar(0.064, 0.025, 0.083, graphite);
    collar(0.035, 0.018, 0.101);
    part(new RoundedBoxGeometry(0.042, 0.006, 0.008, 2, 0.002), graphite, 0, 0, 0.114, tonearm);
    const curve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0, 0.14, 0.095),
      new THREE.Vector3(0, -0.32, 0.095),
      new THREE.Vector3(-0.022, -0.76, 0.085),
      new THREE.Vector3(-0.09, -0.96, 0.068),
      new THREE.Vector3(-0.12, -1.07, 0.058),
    ]);
    part(new THREE.TubeGeometry(curve, 40, 0.022, 12, false), chrome, 0, 0, 0);
    part(new THREE.CylinderGeometry(0.067, 0.067, 0.15, 40), graphite, 0, 0.15, 0.095);
    part(new THREE.CylinderGeometry(0.07, 0.07, 0.024, 40), chrome, 0, 0.23, 0.095);
    part(new RoundedBoxGeometry(0.097, 0.19, 0.05, 3, 0.012), graphite, -0.12, -1.11, 0.056);
    part(new RoundedBoxGeometry(0.072, 0.115, 0.025, 3, 0.006), chrome, -0.12, -1.1, 0.095);
    for (const x of [-0.142, -0.098]) {
      const screw = part(
        new THREE.CylinderGeometry(0.008, 0.008, 0.006, 12),
        graphite,
        x,
        -1.065,
        0.111,
      );
      screw.rotation.x = Math.PI / 2;
    }
    part(new RoundedBoxGeometry(0.059, 0.048, 0.03, 2, 0.005), coral, -0.12, -1.205, 0.038);
    part(new THREE.ConeGeometry(0.007, 0.025, 12), chrome, -0.12, -1.22, 0.015).rotation.x =
      Math.PI;
    const needleMaterials = [chrome, graphite, coral];
    for (const material of needleMaterials) material.transparent = true;
    track(tonearm);
    let needleEngaged = false;
    let needleAmount = 0;
    let needleMotion = { from: 0, to: 0, start: 0 };
    let lastRevealed = false;
    type ScreenPose = { pose: DeckPose; left: number; top: number; unit: number };
    let lastScreen: ScreenPose | undefined;
    let revealRequest:
      { start: number; needleFrom: number; from: ScreenPose | undefined } | undefined;
    let revealCompleted = false;
    let transition: { kind: "reveal" | "return"; start: number; from: ScreenPose } | undefined;
    function inCurrentLayout(snapshot: ScreenPose, rect: DOMRect): DeckPose {
      const factor = unitsPerPixel / snapshot.unit;
      const dx = (snapshot.left - (rect.left + rect.width / 2)) * unitsPerPixel;
      const dy = (rect.top + rect.height / 2 - snapshot.top) * unitsPerPixel;
      return {
        ...snapshot.pose,
        recordX: snapshot.pose.recordX * factor + dx,
        recordY: snapshot.pose.recordY * factor + dy,
        recordScale: snapshot.pose.recordScale * factor,
        sleeveX: snapshot.pose.sleeveX * factor + dx,
        sleeveY: snapshot.pose.sleeveY * factor + dy,
        sleeveScale: snapshot.pose.sleeveScale * factor,
      };
    }
    let halfOffset = 0.5;
    let unitsPerPixel = 1;
    let lastWidth = 0,
      lastHeight = 0;
    function resize(schedule = true) {
      const stage = host!.parentElement!;
      const fallback = stage.querySelector<HTMLElement>(".vinyl-sleeve-reveal__sleeve");
      const stageBounds = stage.getBoundingClientRect();
      // The gameplay entry animation leaves a transformed containing block.
      // Position explicitly from the stage so even that ancestor cannot turn
      // a viewport canvas into a clipped card-sized canvas.
      host!.style.left = `${-stageBounds.left}px`;
      host!.style.top = `${-stageBounds.top}px`;
      host!.style.width = `${window.innerWidth}px`;
      host!.style.height = `${window.innerHeight}px`;
      const width = host!.clientWidth,
        height = host!.clientHeight;
      const stageRect = stage.getBoundingClientRect();
      const canvasRect = host!.getBoundingClientRect();
      if (!width || !height) return;
      unitsPerPixel =
        2 / ((fallback ? Number.parseFloat(getComputedStyle(fallback).width) : 0) || height * 0.82);
      if (fallback)
        halfOffset =
          (stageRect.width / 2 - Number.parseFloat(getComputedStyle(fallback).left)) *
          unitsPerPixel;
      const centerX = stageRect.left + stageRect.width / 2;
      const centerY = stageRect.top + stageRect.height / 2;
      camera.left = (canvasRect.left - centerX) * unitsPerPixel;
      camera.right = (canvasRect.right - centerX) * unitsPerPixel;
      camera.top = (centerY - canvasRect.top) * unitsPerPixel;
      camera.bottom = (centerY - canvasRect.bottom) * unitsPerPixel;
      camera.updateProjectionMatrix();
      if (width !== lastWidth || height !== lastHeight) {
        renderer.setSize(width, height, false);
        lastWidth = width;
        lastHeight = height;
      }
      if (schedule) wake();
    }
    function render(now: number) {
      frame = 0;
      if (disposed || !sleeve || !vinyl) return;
      resize(false);
      const state = stateRef.current;
      const revealReady = state.revealed && (artworkReady || lastRevealed);
      const record = host!.parentElement!.querySelector<HTMLElement>(
        ".vinyl-sleeve-reveal__record",
      );
      const disc = record?.querySelector<HTMLElement>(".vinyl-disc");
      const rect = record?.getBoundingClientRect();
      const stageRect = host!.parentElement!.getBoundingClientRect();
      const playPose: DeckPose = {
        recordX: rect
          ? (rect.left + rect.width / 2 - stageRect.left - stageRect.width / 2) * unitsPerPixel
          : 0,
        recordY: rect
          ? (stageRect.top + stageRect.height / 2 - rect.top - rect.height / 2) * unitsPerPixel
          : 0,
        recordScale: disc
          ? (Number.parseFloat(getComputedStyle(disc).width) * unitsPerPixel) / 1.88
          : 1,
        recordTilt: -0.07,
        sleeveX: -6,
        sleeveY: 0,
        sleeveScale: 1,
        sleeveTurn: 0,
        sleeveRoll: 0,
        assemblyX: 0,
        assemblyY: 0,
      };
      if (state.revealRequested && !revealRequest)
        revealRequest = { start: now, needleFrom: needleAmount, from: lastScreen };
      if (!state.revealRequested) revealRequest = undefined;
      const parkDuration = revealRequest && revealRequest.needleFrom > 0 ? NEEDLE_PARK_DURATION : 0;
      const revealLead = UI_FADE_DURATION + parkDuration + NEEDLE_FADE_DURATION;
      if (revealReady !== lastRevealed) {
        if (lastScreen) {
          transition = {
            kind: revealReady ? "reveal" : "return",
            start: revealReady
              ? Math.max(now, (revealRequest?.start ?? now) + revealLead)
              : now + UI_FADE_DURATION,
            from: revealReady ? (revealRequest?.from ?? lastScreen) : lastScreen,
          };
          revealCompleted = false;
          onReturning(!revealReady);
        }
        if (!revealReady) needleEngaged = false;
        lastRevealed = revealReady;
      }
      let pose =
        state.revealRequested && !revealReady && lastScreen
          ? inCurrentLayout(lastScreen, stageRect)
          : playPose;
      let progress = 1;
      if (transition) {
        progress = reducedMotion.matches
          ? 1
          : Math.min(
              Math.max(
                (now - transition.start) /
                  (transition.kind === "reveal" ? REVEAL_DURATION : RETURN_DURATION),
                0,
              ),
              1,
            );
        const from = inCurrentLayout(transition.from, stageRect);
        pose =
          transition.kind === "reveal"
            ? revealPose(
                from,
                halfOffset,
                progress,
                Math.max(
                  3.8,
                  ((stageRect.left + stageRect.width / 2 + 96) * unitsPerPixel) / 0.7 + 1.5,
                ),
              )
            : returnPose(
                from,
                playPose,
                progress,
                // At minimum framing the complete jacket must clear the left
                // viewport edge. Cubic acceleration adds distance, not time.
                Math.max(
                  4.5,
                  ((stageRect.left + stageRect.width / 2 + 96) * unitsPerPixel) / 0.7 +
                    Math.abs(from.sleeveX) +
                    from.sleeveScale * 1.5,
                ),
              );
      } else if (revealReady) pose = revealPose(playPose, halfOffset, 1);
      // Gently widen the framing during handling, returning to the original
      // scale at both endpoints. This leaves room to extract a record on a
      // narrow screen without any instantaneous camera change.
      const framing = transition ? 1 - 0.3 * Math.sin(Math.PI * progress) : 1;
      pose = {
        ...pose,
        recordX: pose.recordX * framing,
        recordY: pose.recordY * framing,
        recordScale: pose.recordScale * framing,
        sleeveX: pose.sleeveX * framing,
        sleeveY: pose.sleeveY * framing,
        sleeveScale: pose.sleeveScale * framing,
      };
      sleeve.visible =
        (revealReady && now >= (transition?.start ?? 0)) ||
        (transition?.kind === "return" && progress < 1);
      assembly.rotation.set(pose.assemblyX, pose.assemblyY, 0);
      sleeve.position.set(pose.sleeveX, pose.sleeveY, 0);
      sleeve.scale.setScalar(pose.sleeveScale);
      sleeve.rotation.set(0, pose.sleeveTurn, pose.sleeveRoll);
      vinyl.position.set(pose.recordX, pose.recordY, 0);
      vinyl.scale.setScalar(pose.recordScale);
      vinyl.rotation.x = 0;
      vinyl.rotation.y = pose.recordTilt;
      // Framing stays at 1: all changes in apparent size are animated model poses.
      camera.zoom = 1;
      camera.updateProjectionMatrix();
      key.intensity = 1.5;
      if (!state.revealRequested && state.isPlaying && transition?.kind !== "return")
        needleEngaged = true;
      const requestElapsed = revealRequest ? now - revealRequest.start : 0;
      const needleTarget =
        needleEngaged && (!state.revealRequested || requestElapsed < UI_FADE_DURATION) ? 1 : 0;
      const dt = previous ? Math.min((now - previous) / 1000, 0.05) : 0;
      const moving = !!transition && progress < 1;
      const revealing = revealReady && moving && now >= (transition?.start ?? 0);
      if (needleMotion.to !== needleTarget)
        needleMotion = { from: needleAmount, to: needleTarget, start: now };
      const needleProgress = reducedMotion.matches ? 1 : smooth((now - needleMotion.start) / 420);
      needleAmount = needleMotion.from + (needleMotion.to - needleMotion.from) * needleProgress;
      const needleSnapshot = revealRequest?.from ?? transition?.from;
      const needlePose =
        state.revealRequested && needleSnapshot ? inCurrentLayout(needleSnapshot, stageRect) : pose;
      tonearm.position.set(
        needlePose.recordX + needlePose.recordScale * 0.83,
        needlePose.recordY + needlePose.recordScale * 0.72,
        0.25,
      );
      tonearm.scale.setScalar(needlePose.recordScale);
      arm.rotation.z = 0.22 - smooth(needleAmount) * 0.57;
      arm.position.z = Math.sin(Math.PI * needleAmount) * 0.07;
      // Retract before the sleeve arrives; never hover over the album artwork.
      const needleOpacity = state.revealRequested
        ? 1 - smooth((requestElapsed - UI_FADE_DURATION - parkDuration) / NEEDLE_FADE_DURATION)
        : 1;
      for (const material of needleMaterials) material.opacity = needleOpacity;
      tonearm.visible = needleOpacity > 0;
      if (transition?.kind === "return") tonearm.visible = false;
      const turning = state.isPlaying || revealing;
      const targetVelocity = turning && !reducedMotion.matches ? (Math.PI * 2) / 1.8 : 0;
      if (reducedMotion.matches) angularVelocity = 0;
      else {
        angularVelocity +=
          (targetVelocity - angularVelocity) * (1 - Math.exp(-dt / (turning ? 0.12 : 0.2)));
        if (!turning && angularVelocity < 0.002) angularVelocity = 0;
        vinyl.rotation.z = (vinyl.rotation.z - angularVelocity * dt) % (Math.PI * 2);
      }
      // Studio reflections stay fixed while the pressing's actual wear rotates.
      for (const texture of reflectionTextures) {
        // glTF flips V, so matching the angle cancels rotation in world space.
        texture.rotation = vinyl.rotation.z;
        texture.updateMatrix();
      }
      lastScreen = {
        pose,
        left: stageRect.left + stageRect.width / 2,
        top: stageRect.top + stageRect.height / 2,
        unit: unitsPerPixel,
      };
      if (transition?.kind === "reveal" && progress === 1 && !revealCompleted) {
        revealCompleted = true;
        onRevealComplete();
      }
      if (transition?.kind === "return" && progress === 1) {
        transition = undefined;
        onReturning(false);
        updateArtwork();
      }
      previous = now;
      renderer.render(scene, camera);
      if (
        !reducedMotion.matches &&
        (state.isPlaying ||
          moving ||
          needleAmount !== needleTarget ||
          (state.revealRequested && requestElapsed < revealLead) ||
          angularVelocity > 0 ||
          now < layoutUntilRef.current)
      )
        frame = requestAnimationFrame(render);
    }
    function wake() {
      if (!frame && !disposed && ready && sleeve && vinyl && !document.hidden) {
        previous = 0;
        frame = requestAnimationFrame(render);
      }
    }
    function visibilityChanged() {
      if (document.hidden) {
        cancelAnimationFrame(frame);
        frame = 0;
      } else wake();
    }
    function contextLost(event: Event) {
      event.preventDefault();
      ready = false;
      onReady(false);
      onVinylReady(false);
      cancelAnimationFrame(frame);
      frame = 0;
    }
    renderer.domElement.addEventListener("webglcontextlost", contextLost);
    document.addEventListener("visibilitychange", visibilityChanged);
    reducedMotion.addEventListener("change", wake);
    const observer = new ResizeObserver(() => resize());
    observer.observe(host);
    observer.observe(host.parentElement!);
    window.addEventListener("scroll", wake, { passive: true });
    window.addEventListener("resize", wake);
    wakeRef.current = wake;
    async function loadModel(path: string) {
      const asset = await loadSceneModel(path);
      track(asset.scene);
      if (disposed) {
        disposeResources();
        throw new Error("Reveal disposed");
      }
      return asset.scene;
    }
    let artworkReady = false;
    let requestedArtwork: string | null | undefined;
    let artworkRequest = 0;
    function updateArtwork() {
      if (!sleeve || transition?.kind === "return" || (lastRevealed && !stateRef.current.revealed))
        return;
      const url = stateRef.current.artworkUrl;
      if (requestedArtwork === url && artworkRequest) return;
      requestedArtwork = url;
      const request = ++artworkRequest;
      artworkReady = false;
      onReady(false);
      const artwork = sleeve.getObjectByName("SleeveArtwork");
      if (!(artwork instanceof THREE.Mesh)) return;
      if (!url) {
        artworkReady = true;
        onReady(true, url);
        wake();
        return;
      }
      void loadArtworkTexture(url)
        .then((texture) => {
          if (disposed || request !== artworkRequest) {
            texture.dispose();
            return;
          }
          texture.colorSpace = THREE.SRGBColorSpace;
          texture.flipY = false;
          texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 4);
          const material = new THREE.MeshStandardMaterial({
            map: texture,
            roughness: 0.78,
            metalness: 0,
          });
          // Replace each cover without accumulating GPU resources across rounds.
          const oldMaterials = Array.isArray(artwork.material)
            ? artwork.material
            : [artwork.material];
          for (const old of oldMaterials) {
            if (old instanceof THREE.MeshStandardMaterial && old.map) {
              textures.delete(old.map);
              old.map.dispose();
            }
            materials.delete(old);
            old.dispose();
          }
          textures.add(texture);
          materials.add(material);
          artwork.material = material;
          renderer.initTexture(texture);
          artworkReady = true;
          onReady(true, url);
          wake();
        })
        .catch(() => {
          if (!disposed && request === artworkRequest) onReady(false);
        });
    }
    artworkRef.current = updateArtwork;
    async function initialize() {
      const models = await Promise.allSettled([
        loadModel("/models/songuess/sleeve.glb"),
        loadModel("/models/songuess/vinyl.glb"),
      ]);
      if (disposed || models.some((result) => result.status === "rejected")) return;
      sleeve = (models[0] as PromiseFulfilledResult<THREE.Group>).value;
      vinyl = (models[1] as PromiseFulfilledResult<THREE.Group>).value;
      vinyl.traverse((object) => {
        if (!(object instanceof THREE.Mesh)) return;
        for (const material of Array.isArray(object.material)
          ? object.material
          : [object.material]) {
          if (
            !(material instanceof THREE.MeshStandardMaterial) ||
            material.name !== "Vinyl polymer"
          )
            continue;
          for (const texture of [material.map, material.roughnessMap])
            if (texture) {
              texture.center.set(0.5, 0.5);
              reflectionTextures.add(texture);
            }
        }
      });
      assembly.add(sleeve, vinyl);
      updateArtwork();
      if (disposed) return;
      ready = true;
      resize(false);
      // First ready frame already uses the starting pose, avoiding a centered flash.
      render(performance.now());
      onVinylReady(true);
    }
    void initialize().catch(() => {
      if (!disposed) onReady(false);
    });
    return () => {
      disposed = true;
      wakeRef.current = null;
      artworkRef.current = null;
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("scroll", wake);
      window.removeEventListener("resize", wake);
      document.removeEventListener("visibilitychange", visibilityChanged);
      reducedMotion.removeEventListener("change", wake);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      disposeResources();
      environment.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, [onReady, onVinylReady, onReturning, onRevealComplete]);
  return <div className="vinyl-sleeve-reveal__scene" ref={hostRef} aria-hidden="true" />;
}
