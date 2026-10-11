import { useEffect, useRef } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

type Props = {
  artworkUrl?: string | null;
  revealed: boolean;
  revealStartedAt: number;
  isPlaying: boolean;
  onReady: (ready: boolean) => void;
  onVinylReady: (ready: boolean) => void;
};

/** Decorative scene. Round state, audio and accessible controls stay in the DOM. */
export default function VinylRevealScene({
  artworkUrl,
  revealed,
  revealStartedAt,
  isPlaying,
  onReady,
  onVinylReady,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({ revealed, revealStartedAt, isPlaying, artworkUrl });
  const layoutUntilRef = useRef(0);
  const wakeRef = useRef<(() => void) | null>(null);
  const artworkRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (stateRef.current.revealed !== revealed) layoutUntilRef.current = performance.now() + 900;
    stateRef.current = { revealed, revealStartedAt, isPlaying, artworkUrl };
    artworkRef.current?.();
    wakeRef.current?.();
  }, [revealed, revealStartedAt, isPlaying, artworkUrl]);

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
    let halfOffset = 0.5;
    let unitsPerPixel = 1;
    let lastWidth = 0,
      lastHeight = 0;
    function resize(schedule = true) {
      const stage = host!.parentElement!;
      const fallback = stage.querySelector<HTMLElement>(".vinyl-sleeve-reveal__sleeve");
      const width = host!.clientWidth,
        height = host!.clientHeight;
      if (!width || !height) return;
      unitsPerPixel =
        2 / ((fallback ? Number.parseFloat(getComputedStyle(fallback).width) : 0) || height * 0.82);
      if (fallback)
        halfOffset =
          (width / 2 - Number.parseFloat(getComputedStyle(fallback).left)) * unitsPerPixel;
      camera.left = (-width * unitsPerPixel) / 2;
      camera.right = (width * unitsPerPixel) / 2;
      camera.top = (height * unitsPerPixel) / 2;
      camera.bottom = (-height * unitsPerPixel) / 2;
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
      const revealReady = state.revealed && artworkReady;
      const progress = !revealReady
        ? 0
        : reducedMotion.matches
          ? 1
          : Math.min(Math.max((now - state.revealStartedAt) / 1700, 0), 1);
      sleeve.visible = state.revealed && artworkReady;
      const record = host!.parentElement!.querySelector<HTMLElement>(
        ".vinyl-sleeve-reveal__record",
      );
      const disc = record?.querySelector<HTMLElement>(".vinyl-disc");
      const rect = record?.getBoundingClientRect();
      const stageRect = host!.getBoundingClientRect();
      const flourish = Math.min(progress / 0.55, 1);
      const arrival = 1 - Math.pow(1 - flourish, 3);
      const insertion = Math.max((progress - 0.55) / 0.45, 0);
      const tucked = 1 - Math.pow(1 - insertion, 3);
      if (revealReady) {
        // A shared parent guarantees the disc is physically between both panels.
        // The sleeve completes its twirl while the disc is fully outside its mouth.
        assembly.rotation.set(0.018, -0.1, 0);
        sleeve.position.set(
          -halfOffset - (1 - arrival) * 3,
          Math.sin(flourish * Math.PI) * 0.24,
          0,
        );
        sleeve.rotation.set(0, -Math.PI * 2 * (1 - arrival), -Math.sin(flourish * Math.PI) * 0.2);
        vinyl.position.set((halfOffset + 1.08 * (1 - tucked)) * arrival, 0, 0);
        vinyl.rotation.x = 0;
        vinyl.rotation.y = 0;
        camera.zoom = 0.56 + 0.44 * tucked;
        key.intensity = 1.5 + 0.8 * Math.pow(Math.sin(flourish * Math.PI), 6);
      } else {
        assembly.rotation.set(0, 0, 0);
        sleeve.position.set(-5, 0, 0);
        sleeve.rotation.set(0, 0, 0);
        camera.zoom = 1;
        key.intensity = 1.5;
        if (rect)
          vinyl.position.set(
            (rect.left + rect.width / 2 - stageRect.left - stageRect.width / 2) * unitsPerPixel,
            0,
            0,
          );
        vinyl.rotation.x = 0;
        vinyl.rotation.y = -0.07;
      }
      if (disc)
        vinyl.scale.setScalar(
          (Number.parseFloat(getComputedStyle(disc).width) * unitsPerPixel) / 1.88,
        );
      camera.updateProjectionMatrix();
      const dt = previous ? Math.min((now - previous) / 1000, 0.05) : 0;
      const revealing = revealReady && progress < 1;
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
      previous = now;
      renderer.render(scene, camera);
      if (
        !reducedMotion.matches &&
        (state.isPlaying || revealing || angularVelocity > 0 || now < layoutUntilRef.current)
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
    wakeRef.current = wake;
    const loader = new GLTFLoader();
    async function loadModel(path: string) {
      const asset = await loader.loadAsync(path);
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
      if (!sleeve) return;
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
        onReady(true);
        wake();
        return;
      }
      void new THREE.TextureLoader()
        .loadAsync(url)
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
          artworkReady = true;
          onReady(true);
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
      document.removeEventListener("visibilitychange", visibilityChanged);
      reducedMotion.removeEventListener("change", wake);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      disposeResources();
      environment.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, [onReady, onVinylReady]);
  return <div className="vinyl-sleeve-reveal__scene" ref={hostRef} aria-hidden="true" />;
}
