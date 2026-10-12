import { useCallback, useEffect, useRef, useState } from "react";
import type { SetupFilters } from "./SetupWizard";
import {
  RoomPlayback,
  roomFilters,
  roomRequest,
  type RoomSong,
  type RoomAction,
  type RoomResult,
  type RoomState,
  type RoomSummary,
} from "../lib/rooms";

export function useRoom() {
  const [mode, setMode] = useState<
    "choose" | "solo" | "multiplayer" | "create" | "details" | "join" | "summary" | "playing"
  >("choose");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [songCount, setSongCount] = useState(10);
  const [duration, setDuration] = useState(24);
  const [summary, setSummary] = useState<RoomSummary | null>(null);
  const [state, setState] = useState<RoomState | null>(null);
  const [confirmedState, setConfirmedState] = useState<RoomState | null>(null);
  const playback = useRef<RoomPlayback | null>(null);
  const detachedWrites = useRef(new Map<string, Promise<void>>());
  const prefetch = useRef<{
    code: string;
    generation: number;
    startedAt: number;
    resolved: boolean;
    request: Promise<RoomState>;
  } | null>(null);
  const [result, setResult] = useState<RoomResult | null>(null);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const busy = useRef(false);
  const context = useRef(0);
  function loadMember(nextCode: string, generation: number) {
    if (
      prefetch.current?.code === nextCode &&
      prefetch.current.generation === generation &&
      (!prefetch.current.resolved || Date.now() - prefetch.current.startedAt < 15000)
    )
      return prefetch.current.request;
    const writes = detachedWrites.current.get(nextCode);
    const request = writes
      ? writes.then(() => roomRequest<RoomState>(`rooms/${nextCode}/me`))
      : roomRequest<RoomState>(`rooms/${nextCode}/me`);
    prefetch.current = {
      code: nextCode,
      generation,
      startedAt: Date.now(),
      resolved: false,
      request,
    };
    void request.then(
      () => {
        if (prefetch.current?.request === request) prefetch.current.resolved = true;
      },
      () => {},
    );
    void request.catch(() => {
      if (prefetch.current?.request === request) prefetch.current = null;
    });
    return request;
  }
  function detach() {
    const active = playback.current;
    if (active?.isSaving) {
      const writes = active.settled();
      const activeCode = active.snapshot.state.code;
      detachedWrites.current.set(activeCode, writes);
      void writes.then(() => {
        if (detachedWrites.current.get(activeCode) === writes)
          detachedWrites.current.delete(activeCode);
      });
    }
    active?.dispose();
    playback.current = null;
  }
  function install(next: RoomState, generation: number) {
    playback.current?.dispose();
    const controller = new RoomPlayback(next, (snapshot) => {
      if (generation !== context.current) return;
      setState(snapshot.state);
      setConfirmedState(snapshot.confirmedState);
      setResult(snapshot.result);
      setUncertain(snapshot.uncertain);
      setError(snapshot.error);
      if (snapshot.state.closed && !snapshot.result) setMode("summary");
    });
    playback.current = controller;
    setState(next);
    setConfirmedState(next);
    setResult(null);
    setUncertain(false);
  }
  const validName = name.trim().length >= 1 && name.trim().length <= 24;
  const refresh = useCallback(async () => {
    if (!code) return;
    const generation = context.current;
    try {
      const next = await roomRequest<RoomSummary>(`rooms/${code}`);
      if (generation !== context.current) return;
      setSummary(next);
      if (next.is_member && !playback.current?.isSaving) {
        void loadMember(code, generation)
          .then((member) => {
            if (generation === context.current && !playback.current) {
              setState(member);
              setConfirmedState(member);
            }
          })
          .catch(() => {});
      }
      setError("");
    } catch (e) {
      if (generation === context.current)
        setError(e instanceof Error ? e.message : "Could not refresh challenge.");
    }
  }, [code]);
  useEffect(() => {
    const invite = new URLSearchParams(location.search).get("room");
    if (invite) {
      setCode(invite.trim().toUpperCase());
      setMode("summary");
    }
    try {
      setName(localStorage.getItem("songuess.display-name") ?? "");
    } catch {}
  }, []);
  useEffect(() => {
    if (mode !== "summary") return;
    void refresh();
    const check = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const interval = window.setInterval(check, 15000);
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      clearInterval(interval);
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, [mode, refresh]);
  useEffect(
    () => () => {
      context.current += 1;
      detach();
      prefetch.current = null;
    },
    [],
  );
  async function run(task: (generation: number) => Promise<void>) {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    setError("");
    const generation = context.current;
    try {
      await task(generation);
    } catch (e) {
      if (generation === context.current)
        setError(e instanceof Error ? e.message : "Could not reach the challenge.");
    } finally {
      if (generation === context.current) {
        busy.current = false;
        setPending(false);
      }
    }
  }
  function rememberName() {
    try {
      localStorage.setItem("songuess.display-name", name.trim());
    } catch {}
  }
  function open(nextCode: string) {
    const clean = nextCode.trim().toUpperCase();
    if (!/^[A-Z0-9]{8}$/.test(clean)) {
      setError("Enter the 8-character challenge code.");
      return;
    }
    context.current += 1;
    detach();
    prefetch.current = null;
    busy.current = false;
    setPending(false);
    setCode(clean);
    setSummary(null);
    setState(null);
    setConfirmedState(null);
    setResult(null);
    setUncertain(false);
    setMode("summary");
    history.replaceState(null, "", `/?room=${clean}`);
  }
  async function create(filters: SetupFilters) {
    if (!validName) {
      setError("Choose a name with 1–24 characters.");
      return;
    }
    await run(async (generation) => {
      await roomRequest("guest-session", {});
      if (generation !== context.current) return;
      const next = await roomRequest<RoomSummary>("rooms", {
        display_name: name.trim(),
        filters: roomFilters(filters),
        song_count: songCount,
        duration_hours: duration,
      });
      if (generation !== context.current) return;
      rememberName();
      setCode(next.code);
      setSummary(next);
      setMode("summary");
      history.replaceState(null, "", `/?room=${next.code}`);
    });
  }
  async function resume() {
    const current = playback.current;
    if (
      current &&
      !current.snapshot.uncertain &&
      !current.snapshot.state.closed &&
      Date.now() < Date.parse(current.snapshot.state.closes_at)
    ) {
      current.next();
      setMode(current.snapshot.state.current_round ? "playing" : "summary");
      return;
    }
    await run(async (generation) => {
      // A resume read must follow every accepted background write.
      const active = playback.current;
      // A blocked or expired controller needs a new read even if the summary
      // prefetched an earlier version before the failed write.
      if (active) prefetch.current = null;
      if (active?.isSaving) {
        await active.settled();
        if (generation !== context.current) return;
        prefetch.current = null;
      }
      let next = await loadMember(code, generation);
      if (generation !== context.current) return;
      prefetch.current = null;
      if (!next.closed && Date.now() >= Date.parse(next.closes_at)) {
        next = await roomRequest<RoomState>(`rooms/${code}/me`);
        if (generation !== context.current) return;
      }
      install(next, generation);
      setMode(next.closed || !next.current_round ? "summary" : "playing");
    });
  }
  async function join() {
    if (!validName) {
      setError("Choose a name with 1–24 characters.");
      return;
    }
    await run(async (generation) => {
      await roomRequest("guest-session", {});
      if (generation !== context.current) return;
      const next = await roomRequest<RoomState>(`rooms/${code}/join`, {
        display_name: name.trim(),
      });
      if (generation !== context.current) return;
      rememberName();
      install(next, generation);
      setMode(next.closed || !next.current_round ? "summary" : "playing");
    });
  }
  function action(action: RoomAction, songId?: number, guess?: RoomSong) {
    prefetch.current = null;
    playback.current?.action(action, songId, guess);
  }
  function next() {
    const controller = playback.current;
    if (!controller || controller.snapshot.uncertain) return;
    controller.next();
    if (!controller.snapshot.state.current_round || controller.snapshot.state.closed)
      setMode("summary");
  }
  function exit() {
    setMode("summary");
  }
  function resetMode(next: "choose" | "solo" | "multiplayer") {
    context.current += 1;
    detach();
    prefetch.current = null;
    busy.current = false;
    setPending(false);
    setMode(next);
    setCode("");
    setState(null);
    setConfirmedState(null);
    setSummary(null);
    setResult(null);
    setUncertain(false);
    setError("");
    history.replaceState(null, "", "/");
  }
  function solo() {
    resetMode("solo");
  }
  function home() {
    resetMode("choose");
  }
  function multiplayer() {
    resetMode("multiplayer");
  }
  return {
    mode,
    setMode,
    code,
    name,
    setName,
    songCount,
    setSongCount,
    duration,
    setDuration,
    summary,
    state,
    confirmedState,
    result,
    pending,
    uncertain,
    error,
    validName,
    refresh,
    open,
    create,
    resume,
    join,
    action,
    next,
    exit,
    solo,
    home,
    multiplayer,
  };
}
export type RoomController = ReturnType<typeof useRoom>;
