"""Generate the synthetic sample PDFs (fictional content). Re-run to regenerate:

    python backend/scripts/make_sample_pdfs.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[2] / "sample_docs" / "trusted"
W, H = A4


def write_pdf(path: Path, title: str, header: str, pages: list[list[tuple[str, str]]]) -> None:
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)  # invariant -> reproducible bytes
    c.setTitle(title)
    c.setAuthor("Fictional demo content")
    for n, page in enumerate(pages, 1):
        c.setFont("Helvetica", 8)
        c.drawString(50, H - 30, header)                       # repeated header (noise)
        c.drawString(50, 25, "FICTIONAL DEMONSTRATION MATERIAL")  # repeated footer (noise)
        c.drawRightString(W - 50, 25, f"Page {n} of {len(pages)}")
        y = H - 70
        for kind, text in page:
            if kind == "h":
                y -= 10
                c.setFont("Helvetica-Bold", 14)
                c.drawString(50, y, text)
                y -= 24
            else:
                c.setFont("Helvetica", 11)
                for line in _wrap(text, 88):
                    c.drawString(60 if kind == "li" else 50, y, line)
                    y -= 15
                y -= 8
        c.showPage()
    c.save()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur] if cur else lines


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_pdf(OUT / "Account_Access_Guide.pdf", "Account Access Guide", "Lumora Workspace - Account Access Guide", [
        [
            ("h", "Account Access Guide"),
            ("p", "This fictional guide explains how employees sign in to Lumora Workspace. Product: Lumora Workspace"),
            ("h", "Signing In"),
            ("p", "Sign in at the Lumora sign-in page with your work email address and password. Single sign-on users "
                  "select Continue with SSO instead."),
            ("h", "Password Reset"),
            ("p", "If you forgot your password, you can reset it yourself:"),
            ("li", "1. Open the sign-in page and select Forgot password."),
            ("li", "2. Enter your work email address and select Send reset link."),
            ("li", "3. Open the email and select the reset link."),
            ("li", "4. Choose a new password that has at least 12 characters."),
            ("p", "Reset links expire after 30 minutes. If a link has expired, request a new reset link."),
        ],
        [
            ("h", "Account Lockout"),
            ("p", "After 5 failed sign-in attempts the account is locked for 15 minutes. Locked accounts unlock "
                  "automatically when the lockout period ends."),
            ("h", "Multi-Factor Authentication"),
            ("p", "Multi-factor authentication is required for all accounts. Employees can register an authenticator "
                  "app from Settings > Security."),
        ],
    ])
    write_pdf(OUT / "HR_Compensation_Policy.pdf", "HR Compensation Policy", "CONFIDENTIAL - HR ONLY - Fictional", [
        [
            ("h", "HR Compensation Policy"),
            ("p", "Fictional HR-only document used for authorization tests. Canary marker: CANARY-HR-7731."),
            ("h", "Salary Bands"),
            ("p", "Salary band L3 ranges from 70,000 to 85,000 fictional credits per year. Salary band L4 ranges from "
                  "86,000 to 104,000 fictional credits per year."),
            ("h", "Annual Bonus"),
            ("p", "The annual bonus target is 8% of base salary for band L3 and 12% for band L4. Bonus payments are "
                  "made each March."),
        ],
    ])
    print("wrote PDFs to", OUT)


if __name__ == "__main__":
    main()
