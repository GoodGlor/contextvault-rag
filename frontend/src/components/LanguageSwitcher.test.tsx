import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useTranslation } from "react-i18next";
import { LanguageSwitcher } from "./LanguageSwitcher";

/** A tiny probe that shows a translated string so we can observe the language flip. */
function Probe() {
  const { t } = useTranslation();
  return <p>{t("login.title")}</p>;
}

describe("LanguageSwitcher", () => {
  it("switches the UI language between English and Ukrainian", async () => {
    // The test harness pins the locale to English before each test.
    render(
      <>
        <LanguageSwitcher />
        <Probe />
      </>,
    );
    expect(screen.getByText("Sign in")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Language"), "uk");

    // The probe re-renders in Ukrainian, and the switcher's own label localizes too.
    expect(await screen.findByText("Увійти")).toBeInTheDocument();
    expect(screen.getByLabelText("Мова")).toBeInTheDocument();
  });
});
