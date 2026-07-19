#!/usr/bin/env python3
"""Create PDF document from the markdown specification"""
import os
import sys

def main():
    pdf_file = r"D:\AutoTrade\trading_system_summary.pdf"
    md_file = r"D:\AutoTrade\trading_system_summary.md"

    # Try using available PDF libraries in order of preference

    # Method 1: fpdf2
    try:
        from fpdf import FPDF
        print("Creating PDF with fpdf2...", file=sys.stderr)

        pdf = FPDF(format='letter', unit='mm', compress=True)
        pdf.add_page()

        # Set margins
        pdf.set_margins(15, 15, 15)
        pdf.set_auto_page_break(auto=True, margin=15)

        # Title
        pdf.set_font("Arial", "B", 18)
        pdf.cell(0, 10, "Automated Gold & Forex Trading System", 0, 1, 'C')

        # Subtitle
        pdf.set_font("Arial", "I", 11)
        pdf.cell(0, 8, "Decision Brief for Non-Technical Stakeholders", 0, 1, 'C')
        pdf.ln(5)

        # Content
        pdf.set_font("Arial", "", 10)

        with open(md_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for line in lines:
            line = line.rstrip('\n')

            if line.startswith('# '):
                pdf.set_font("Arial", "B", 14)
                pdf.multi_cell(0, 7, line[2:].strip())
                pdf.ln(2)
                pdf.set_font("Arial", "", 10)
            elif line.startswith('## '):
                pdf.set_font("Arial", "B", 12)
                pdf.multi_cell(0, 6, line[3:].strip())
                pdf.ln(1)
                pdf.set_font("Arial", "", 10)
            elif line.startswith('### '):
                pdf.set_font("Arial", "B", 11)
                pdf.multi_cell(0, 6, line[4:].strip())
                pdf.set_font("Arial", "", 10)
            elif line.startswith('| '):
                # Skip table lines for simplicity
                continue
            elif line.startswith('- ') or line.startswith('* '):
                pdf.multi_cell(0, 5, '  ' + line[2:].strip())
            elif line.startswith('1. ') or (len(line) > 2 and line[0].isdigit() and line[1:3] == '. '):
                pdf.multi_cell(0, 5, line.strip())
            elif line.startswith('---'):
                pdf.ln(3)
            elif line.strip():
                pdf.multi_cell(0, 5, line.strip())
            else:
                pdf.ln(2)

        pdf.output(pdf_file)
        print(f"✓ PDF created: {pdf_file}", file=sys.stderr)
        return True
    except ImportError:
        pass
    except Exception as e:
        print(f"fpdf2 error: {e}", file=sys.stderr)

    # Method 2: reportlab
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import inch
        print("Creating PDF with reportlab...", file=sys.stderr)

        c = canvas.Canvas(pdf_file, pagesize=letter)
        width, height = letter

        y = height - 0.75 * inch
        c.setFont("Helvetica-Bold", 16)
        c.drawString(0.75 * inch, y, "Automated Gold & Forex Trading System")

        y -= 0.3 * inch
        c.setFont("Helvetica-Oblique", 11)
        c.drawString(0.75 * inch, y, "Decision Brief for Non-Technical Stakeholders")

        y -= 0.4 * inch
        c.setFont("Helvetica", 10)

        with open(md_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for line in lines[:200]:
            line = line.rstrip('\n').strip()
            if not line or line.startswith('|'):
                y -= 0.1 * inch
                continue

            if line.startswith('# ') or line.startswith('## '):
                y -= 0.2 * inch

            if y < 0.75 * inch:
                c.showPage()
                y = height - 0.75 * inch
                c.setFont("Helvetica", 10)

            text = line[:100]
            if line.startswith('- '):
                text = '  ' + line[2:][:95]

            c.drawString(0.75 * inch, y, text)
            y -= 0.15 * inch

        c.save()
        print(f"✓ PDF created: {pdf_file}", file=sys.stderr)
        return True
    except ImportError:
        pass
    except Exception as e:
        print(f"reportlab error: {e}", file=sys.stderr)

    # Method 3: Create minimal valid PDF
    print("Creating minimal PDF...", file=sys.stderr)
    try:
        pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources 4 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>
endobj
4 0 obj
<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>
endobj
5 0 obj
<< /Length 1400 >>
stream
BT
/F1 20 Tf
50 720 Td
(Automated Gold & Forex Trading System) Tj
0 -40 Td
/F1 12 Tf
(Decision Brief for Non-Technical Stakeholders) Tj
0 -45 Td
/F1 11 Tf
(SYSTEM OVERVIEW) Tj
0 -30 Td
/F1 10 Tf
(An automated trading system for Gold and Forex that works like a 5-person team:) Tj
0 -25 Td
(- The Brain: AI Council debates every trade with 3 analytical voices) Tj
0 -20 Td
(- The Shield: Portfolio-level risk checkpoint) Tj
0 -20 Td
(- The CFO: Intelligent position sizing and money management) Tj
0 -20 Td
(- The Watchman: Active position monitoring) Tj
0 -20 Td
(- The Auditor: Daily performance review and strategy approval) Tj
0 -40 Td
/F1 11 Tf
(SAFETY-FIRST APPROACH) Tj
0 -30 Td
/F1 10 Tf
(Nothing goes live until proven three times over:) Tj
0 -25 Td
(1. Historical backtest on data never seen during development) Tj
0 -20 Td
(2. Weeks of paper trading - live conditions, zero risk) Tj
0 -20 Td
(3. Gradual live trading ramp starting at 0.25% per trade) Tj
0 -40 Td
/F1 11 Tf
(INFRASTRUCTURE COST) Tj
0 -30 Td
/F1 10 Tf
(Approximately $15-60 per month when fully live:) Tj
0 -25 Td
(- Cloud server (VPS): $10-30/month) Tj
0 -20 Td
(- AI service (Claude API): $5-30/month) Tj
0 -20 Td
(- Market data: Free) Tj
0 -20 Td
(- Platform & monitoring: Free) Tj
0 -40 Td
(Note: This is separate from trading capital and trading losses) Tj
0 -30 Td
(For complete details, see: trading_system_summary.html or spec.md) Tj
ET
endstream
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000203 00000 n
0000000290 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
1741
%%EOF
"""

        with open(pdf_file, 'wb') as f:
            f.write(pdf_content)

        print(f"✓ PDF created (basic format): {pdf_file}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"Minimal PDF error: {e}", file=sys.stderr)
        return False

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
