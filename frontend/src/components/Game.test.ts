import { describe, expect, it } from "vitest";

import { countryFlag, filterCountryOptions, type CountryOption } from "./Game";

const countries: CountryOption[] = [
  { code: "CL", name: "Chile", flag: "🇨🇱" },
  { code: "CN", name: "China", flag: "🇨🇳" },
  { code: "US", name: "United States", flag: "🇺🇸" },
];

describe("country origin lookup", () => {
  it("matches prefixes against full country names", () => {
    expect(filterCountryOptions(countries, "chi").map(({ code }) => code)).toEqual(["CL", "CN"]);
  });

  it("matches ISO country-code prefixes and ignores surrounding space", () => {
    expect(filterCountryOptions(countries, " us ").map(({ code }) => code)).toEqual(["US"]);
  });

  it("renders ISO country codes as flags", () => {
    expect(countryFlag("cl")).toBe("🇨🇱");
  });
});
