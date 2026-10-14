# services/receipt_pdf.py
import io
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

DARK_GREEN = HexColor("#033126")
ORANGE = HexColor("#DD8550")
CREAM_BG = HexColor("#FBEFE4")
GREY_TEXT = HexColor("#4A4A4A")
LINE_GREY = HexColor("#D9D9D9")

# Same asset used in the "welcome" email header band.
DEFAULT_LOGO_PATH = os.path.join(
    os.path.dirname(__file__), "assets", "adaptive-icon.png"
)

TAGLINE = "Legal Support Made Simple"

# RevenueCat gives us an ISO currency code (e.g. "GBP", "USD", "EUR"), not a
# symbol. Map the common ones we actually see from App Store / Play Store
# purchases; anything unmapped falls back to "<CODE> " (e.g. "AUD 4.99")
# rather than guessing a symbol that might be wrong.
CURRENCY_SYMBOLS = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "CAD": "CA$",
    "AUD": "A$",
    "NZD": "NZ$",
}


def get_currency_symbol(currency_code):
    """Return a display symbol for an ISO currency code, with a safe fallback."""
    if not currency_code:
        return "£"  # default assumption: LawNeeds' primary market is UK
    code = currency_code.strip().upper()
    return CURRENCY_SYMBOLS.get(code, f"{code} ")


def generate_receipt_pdf(
    purchase_type,
    order_id,
    date,
    amount_paid,
    support_contact="support@lawneeds.co.uk",
    total_paid=None,
    currency_symbol="£",
    logo_path=DEFAULT_LOGO_PATH,
):
    """
    Generates a LawNeeds payment receipt PDF for an IAP (App Store / Google
    Play) purchase. No payment method, subtotal, or VAT breakdown is shown
    since that data isn't available from RevenueCat purchases — only a
    single total amount paid, in whatever currency the store charged.

    The header mirrors the "welcome" email band: logo + "LawNeeds" title
    with the "Legal Support Made Simple" tagline underneath.

    Returns raw PDF bytes, suitable for attaching to an email.
    """
    buffer = io.BytesIO()
    width, height = A4
    c = canvas.Canvas(buffer, pagesize=A4)

    # --- Header band ---
    header_h = 42 * mm
    c.setFillColor(CREAM_BG)
    c.rect(0, height - header_h, width, header_h, fill=1, stroke=0)

    # Logo (left of the title, vertically centred against the title+tagline
    # block — same pairing as the email header).
    logo_size = 17 * mm
    logo_top_pad = 16 * mm  # gap from top of page to top of logo
    logo_x = 20 * mm
    title_x = logo_x
    logo_drawn = False
    if logo_path and os.path.isfile(logo_path):
        try:
            logo = ImageReader(logo_path)
            logo_y = height - logo_top_pad - logo_size
            c.drawImage(
                logo,
                logo_x,
                logo_y,
                width=logo_size,
                height=logo_size,
                mask="auto",
                preserveAspectRatio=True,
            )
            title_x = logo_x + logo_size + 4 * mm
            logo_drawn = True
        except Exception:
            # Fall back to a text-only header if the asset can't be read.
            logo_drawn = False

    c.setFillColor(DARK_GREEN)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(title_x, height - 26 * mm, "LawNeeds")

    # Tagline, directly under the title (matches the email band's motto).
    c.setFillColor(ORANGE)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(title_x, height - 30.5 * mm, TAGLINE)

    c.setFillColor(GREY_TEXT)
    c.setFont("Helvetica", 8)
    addr_lines = [
        "71-75 Shelton Street,",
        "Covent Garden, London,",
        "United Kingdom, WC2H 9JQ",
        "T: +44 7466 417 889",
        "lawneeds.co.uk",
    ]
    y = height - 12 * mm
    for line in addr_lines:
        c.drawRightString(width - 20 * mm, y, line)
        y -= 3.8 * mm

    # Orange divider
    c.setFillColor(ORANGE)
    c.rect(0, height - header_h - 1.5 * mm, width, 1.5 * mm, fill=1, stroke=0)

    # --- Title ---
    c.setFillColor(DARK_GREEN)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(20 * mm, height - 60 * mm, "Payment receipt")

    c.setFillColor(ORANGE)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, height - 68 * mm, "A summary of your in-app purchase")

    # --- Detail rows ---
    rows = [
        ("Purchase type", purchase_type),
        ("Order ID", order_id),
        ("Date", date),
        ("Amount paid", amount_paid),
        ("Support contact", support_contact),
    ]

    row_y = height - 78 * mm
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.75)
    c.line(20 * mm, row_y, width - 20 * mm, row_y)

    row_h = 11 * mm
    for label, value in rows:
        row_y -= row_h
        c.setFillColor(DARK_GREEN)
        c.setFont("Helvetica-Bold", 10.5)
        c.drawString(20 * mm, row_y + 3 * mm, label)

        c.setFillColor(ORANGE)
        c.setFont("Helvetica", 10.5)
        c.drawString(65 * mm, row_y + 3 * mm, "|")

        c.setFillColor(GREY_TEXT)
        c.setFont("Helvetica", 10.5)
        c.drawString(70 * mm, row_y + 3 * mm, str(value) if value is not None else "-")

        c.setStrokeColor(LINE_GREY)
        c.line(20 * mm, row_y, width - 20 * mm, row_y)

    # --- Total bar (dark green, full width - label left, amount right) ---
    box_top = row_y - 12 * mm
    box_h = 24 * mm
    box_x = 20 * mm
    box_w = width - 40 * mm

    c.setFillColor(DARK_GREEN)
    c.rect(box_x, box_top - box_h, box_w, box_h, fill=1, stroke=0)

    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(box_x + 8 * mm, box_top - (box_h / 2) - 1.5 * mm, "TOTAL PAID")

    total_str = f"{currency_symbol}{total_paid}" if total_paid is not None else "-"
    c.setFont("Helvetica-Bold", 20)
    c.drawRightString(box_x + box_w - 8 * mm, box_top - (box_h / 2) - 3 * mm, total_str)

    # --- Footer ---
    footer_y = box_top - box_h - 14 * mm
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.75)
    c.line(20 * mm, footer_y, width - 20 * mm, footer_y)

    c.setFillColor(DARK_GREEN)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, footer_y - 7 * mm, "Thank you for choosing us.")

    c.setFillColor(GREY_TEXT)
    c.setFont("Helvetica", 8.5)
    c.drawString(20 * mm, footer_y - 12.5 * mm, "Your trust matters. If you have any questions,")
    c.drawString(20 * mm, footer_y - 16.5 * mm, "our support team is here to help.")

    c.setFont("Helvetica", 7.5)
    c.drawCentredString(
        width / 2,
        15 * mm,
        "LawNeeds Ltd is a limited company registered in England & Wales (Company No. 15610058).",
    )

    c.showPage()
    c.save()

    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes