import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import Tutorial, { hasSeenTutorial, rememberTutorial, TUTORIAL_STORAGE_KEY } from "./Tutorial";

describe("tutorial persistence", () => {
  it("only treats the current stored marker as dismissed", () => {
    expect(hasSeenTutorial({ getItem: () => null })).toBe(false);
    expect(hasSeenTutorial({ getItem: () => "true" })).toBe(true);
  });

  it("writes a long-lived browser marker", () => {
    const setItem = vi.fn();
    rememberTutorial({ setItem });
    expect(setItem).toHaveBeenCalledWith(TUTORIAL_STORAGE_KEY, "true");
  });

  it("presents Start listening as the sole dismissal action", () => {
    const html = renderToStaticMarkup(createElement(Tutorial, { onDismiss: vi.fn() }));

    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain("Start listening");
    expect(html).not.toContain("PLAYER NOTES");
    expect(html).not.toContain("tutorial-close");
  });
});
