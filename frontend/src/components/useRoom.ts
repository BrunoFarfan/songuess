import { useCallback, useEffect, useRef, useState } from "react";
import type { SetupFilters } from "./SetupWizard";
import {
  RoomApiError,
  roomFilters,
  roomRequest,
  submitRoomAction,
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
  const [result, setResult] = useState<RoomResult | null>(null);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const busy = useRef(false);
  const context = useRef(0);
  const validName = name.trim().length >= 1 && name.trim().length <= 24;
  const refresh = useCallback(async () => {
    if (!code) return;
    const generation = context.current;
    try {
      const next = await roomRequest<RoomSummary>(`rooms/${code}`);
      if (generation !== context.current) return;
      setSummary(next);
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
      busy.current = false;
      setPending(false);
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
    setCode(clean);
    setSummary(null);
    setState(null);
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
    await run(async (generation) => {
      const next = await roomRequest<RoomState>(`rooms/${code}/me`);
      if (generation !== context.current) return;
      setState(next);
      setResult(null);
      setUncertain(false);
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
      setState(next);
      setResult(null);
      setUncertain(false);
      setMode(next.closed || !next.current_round ? "summary" : "playing");
    });
  }
  async function action(action: RoomAction, songId?: number) {
    const round = state?.current_round;
    if (!round || uncertain) return;
    await run(async (generation) => {
      let next;
      try {
        next = await submitRoomAction(code, round, action, songId);
      } catch (error) {
        if (generation !== context.current) return;
        setUncertain(
          !(error instanceof RoomApiError && error.status < 500 && error.status !== 409),
        );
        throw error;
      }
      if (generation !== context.current) return;
      setState(next.state);
      setUncertain(false);
      const completed = next.state.results.find((item) => item.position === round.position);
      setResult(completed ?? null);
      if (next.recovered)
        setError("Progress refreshed. Review the current clue before continuing.");
      if (next.state.closed && !completed) setMode("summary");
    });
  }
  function exit() {
    setMode("summary");
  }
  function resetMode(next: "choose" | "solo" | "multiplayer") {
    context.current += 1;
    setMode(next);
    setCode("");
    setState(null);
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
    exit,
    solo,
    home,
    multiplayer,
  };
}
export type RoomController = ReturnType<typeof useRoom>;
