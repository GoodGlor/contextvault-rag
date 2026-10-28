import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReportMetaGroup } from "./ReportMetaGroup";

describe("ReportMetaGroup", () => {
  it("counts blocks and sections", () => {
    render(<ReportMetaGroup blocks={7} sections={3} language="uk" />);

    expect(screen.getByTestId("meta-blocks")).toHaveTextContent("7");
    expect(screen.getByTestId("meta-sections")).toHaveTextContent("3");
  });

  it("names the language the report was written in", () => {
    render(<ReportMetaGroup blocks={1} sections={1} language="uk" />);

    expect(screen.getByTestId("meta-language")).toHaveTextContent("Ukrainian");
  });

  it("never shows a page count — nothing in the editor can know one", () => {
    // Spec decision 3: a page count needs WeasyPrint's line breaking. Neither
    // this editor nor the canvas #156 adds can compute one, and a guessed page
    // number in the editor of a filed record is a lie the reader cannot catch.
    render(<ReportMetaGroup blocks={7} sections={3} language="uk" />);

    expect(screen.queryByTestId("meta-pages")).not.toBeInTheDocument();
    expect(screen.queryByText(/page/i)).not.toBeInTheDocument();
  });

  it("omits the language row rather than guessing when the report has no plan", () => {
    render(<ReportMetaGroup blocks={1} sections={0} language={null} />);

    expect(screen.queryByTestId("meta-language")).not.toBeInTheDocument();
    expect(screen.getByTestId("meta-blocks")).toBeInTheDocument();
  });

  it("shows an unknown language code as itself rather than blank", () => {
    render(<ReportMetaGroup blocks={1} sections={1} language="pl" />);

    expect(screen.getByTestId("meta-language")).toHaveTextContent("pl");
  });
});
