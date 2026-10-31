import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { safeHref } from "../api";
import { AnswerView } from "../components/AnswerView";
import type { AskResponse } from "../types";

afterEach(cleanup);

function response(over: Partial<AskResponse>): AskResponse {
  return {
    id: "r1", question: "q", state: "answered", answer: [], message: null, missing_information: [],
    clarification_question: null, conflicts: [], citations: [], warnings: [], generator: "test",
    mock_mode: false, security_flags: [], cached: false, ...over,
  };
}

const citation = {
  label: "P1", number: 1, chunk_id: "doc-1:c2", document_id: "doc-1", title: "Account Access Guide",
  section: "Account Access Guide > Password Reset", page: 1, section_anchor: "password-reset-1-p1",
  product_version: null, effective_date: null, source_id: "upload://doc-1/Account_Access_Guide.pdf",
  excerpt: "Reset links expire after 30 minutes.", passage_url: "/api/passages/doc-1:c2",
  download_url: "/api/documents/doc-1/download", injection_flag: false,
};

describe("AnswerView", () => {
  it("shows the answer with an expandable citation including page and excerpt", () => {
    render(<AnswerView result={response({ answer: [{ text: "Reset links expire after 30 minutes.", citations: [1] }], citations: [citation] })} />);
    expect(screen.getByTestId("state-badge").textContent).toBe("Answered");
    fireEvent.click(screen.getByLabelText("Show citation 1"));
    expect(screen.getByText(/page 1/)).toBeTruthy();
    expect(screen.getAllByText("Reset links expire after 30 minutes.").length).toBe(2);
  });

  it("renders conflict and clarification states visibly", () => {
    render(<AnswerView result={response({
      state: "clarification_needed",
      clarification_question: "Which product version are you using?",
      conflicts: [{ kind: "version_difference", description: "d", citations: [1, 2],
        facts: [{ value: "20 MB", title: "Manual", version: "2" }, { value: "50 MB", title: "Release Notes", version: "3" }] }],
    })} />);
    expect(screen.getByTestId("state-badge").textContent).toBe("Clarification needed");
    expect(screen.getByText("Which product version are you using?")).toBeTruthy();
    expect(screen.getByText("Differs by version")).toBeTruthy();
    expect(screen.getByText("20 MB")).toBeTruthy();
    expect(screen.getByText("50 MB")).toBeTruthy();
  });

  it("shows not-found and mock-mode labelling", () => {
    render(<AnswerView result={response({ state: "not_found", mock_mode: true,
      message: "I could not find that information in the documents available to you." })} />);
    expect(screen.getByTestId("state-badge").textContent).toBe("Not found");
    expect(screen.getByText(/MOCK MODE/)).toBeTruthy();
  });

  it("never interprets document text as HTML", () => {
    const evil = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';
    const { container } = render(<AnswerView result={response({ answer: [{ text: evil, citations: [] }] })} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>");
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
  });
});

describe("safeHref", () => {
  it("rejects unsafe link schemes", () => {
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,<script>")).toBeNull();
    expect(safeHref("//evil.example/x")).toBeNull();
    expect(safeHref("https://example.com")).toBe("https://example.com");
    expect(safeHref("/api/passages/x")).toBe("/api/passages/x");
  });
});
