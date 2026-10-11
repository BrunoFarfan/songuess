import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { PrefetchCache } from "./prefetchCache";

const images = new PrefetchCache<HTMLImageElement>(80);
const models = new PrefetchCache<ArrayBuffer>(2);

export function preloadArtwork(url: string): Promise<HTMLImageElement> {
  return images.get(url, async () => {
    const image = await new THREE.ImageLoader().loadAsync(url);
    await image.decode();
    return image;
  });
}

export async function loadArtworkTexture(url: string): Promise<THREE.Texture> {
  const texture = new THREE.Texture(await preloadArtwork(url));
  texture.needsUpdate = true;
  return texture;
}

function preloadModel(path: string) {
  return models.get(path, async () => {
    const response = await fetch(path);
    if (!response.ok) throw new Error("Could not load the record models.");
    return response.arrayBuffer();
  });
}

export async function loadSceneModel(path: string) {
  // Parse independently so each scene owns and can dispose its GPU resources.
  return new GLTFLoader().parseAsync(await preloadModel(path), "/models/songuess/");
}

export function preloadSceneModels() {
  return Promise.all([
    preloadModel("/models/songuess/sleeve.glb"),
    preloadModel("/models/songuess/vinyl.glb"),
  ]);
}
