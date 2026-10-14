"""
PDF Generation Service for LawNeeds
Generates professional PDF documents for chat histories and other legal documents
"""

import io
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import Color, black, HexColor
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib import colors

logger = logging.getLogger(__name__)


class PDFService:
    """Service for generating PDF documents"""
    
    # Color scheme
    PRIMARY_COLOR = HexColor('#1E3A8A')  # Dark blue
    SECONDARY_COLOR = HexColor('#3B82F6')  # Medium blue
    USER_COLOR = HexColor('#10B981')  # Green for user messages
    AI_COLOR = HexColor('#6366F1')  # Indigo for AI messages
    LIGHT_GRAY = HexColor('#F3F4F6')
    
    def __init__(self):
        """Initialize PDF service with custom styles"""
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
    
    def _setup_custom_styles(self):
        """Setup custom paragraph styles"""
        # Title style
        self.styles.add(ParagraphStyle(
            name='CustomTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            textColor=self.PRIMARY_COLOR,
            spaceAfter=30,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        ))
        
        # Subtitle style
        self.styles.add(ParagraphStyle(
            name='CustomSubtitle',
            parent=self.styles['Heading2'],
            fontSize=16,
            textColor=self.SECONDARY_COLOR,
            spaceAfter=12,
            spaceBefore=12,
            fontName='Helvetica-Bold'
        ))
        
        # Metadata style
        self.styles.add(ParagraphStyle(
            name='Metadata',
            parent=self.styles['Normal'],
            fontSize=10,
            textColor=HexColor('#4B5563'),
            spaceAfter=6
        ))
        
        # User message style
        self.styles.add(ParagraphStyle(
            name='UserMessage',
            parent=self.styles['Normal'],
            fontSize=11,
            textColor=black,
            leftIndent=20,
            rightIndent=80,
            spaceAfter=8,
            spaceBefore=4
        ))
        
        # AI message style
        self.styles.add(ParagraphStyle(
            name='AIMessage',
            parent=self.styles['Normal'],
            fontSize=11,
            textColor=black,
            leftIndent=80,
            rightIndent=20,
            spaceAfter=8,
            spaceBefore=4
        ))
        
        # Timestamp style
        self.styles.add(ParagraphStyle(
            name='Timestamp',
            parent=self.styles['Normal'],
            fontSize=8,
            textColor=HexColor('#9CA3AF'),
            leftIndent=20,
            spaceAfter=2
        ))
        
        # Footer style
        self.styles.add(ParagraphStyle(
            name='Footer',
            parent=self.styles['Normal'],
            fontSize=8,
            textColor=HexColor('#6B7280'),
            alignment=TA_CENTER
        ))
    
    def generate_chat_history_pdf(
        self,
        chat_history: List[Dict[str, Any]],
        referral_metadata: Optional[Dict[str, Any]] = None
    ) -> bytes:
        """
        Generate a professional PDF document from chat history
        
        Args:
            chat_history: List of chat messages with role, content, timestamp
            referral_metadata: Optional metadata about the referral (client name, legal area, etc.)
        
        Returns:
            PDF content as bytes
        """
        try:
            # Create a buffer to store PDF
            buffer = io.BytesIO()
            
            # Create the PDF document
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=72,
                leftMargin=72,
                topMargin=72,
                bottomMargin=72
            )
            
            # Container for the 'Flowable' objects
            story = []
            
            # Add header
            self._add_header(story, referral_metadata)
            
            # Add metadata section
            if referral_metadata:
                self._add_metadata_section(story, referral_metadata)
            
            # Add separator
            story.append(Spacer(1, 0.3 * inch))
            story.append(self._create_separator_line())
            story.append(Spacer(1, 0.3 * inch))
            
            # Add conversation section
            self._add_conversation_section(story, chat_history)
            
            # Add footer
            story.append(Spacer(1, 0.5 * inch))
            story.append(self._create_separator_line())
            story.append(Spacer(1, 0.2 * inch))
            self._add_footer(story)
            
            # Build PDF
            doc.build(story, onFirstPage=self._add_page_number, onLaterPages=self._add_page_number)
            
            # Get PDF bytes
            pdf_bytes = buffer.getvalue()
            buffer.close()
            
            logger.info(f"Successfully generated PDF with {len(chat_history)} messages")
            return pdf_bytes
            
        except Exception as e:
            logger.error(f"Error generating PDF: {e}")
            raise

    def generate_service_request_pdf(
        self,
        request_details: List[Dict[str, Any]],
        referral_metadata: Optional[Dict[str, Any]] = None
    ) -> bytes:
        """Generate a PDF manuscript for a direct service request."""
        try:
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                rightMargin=72,
                leftMargin=72,
                topMargin=72,
                bottomMargin=72
            )

            story = []
            self._add_header(
                story,
                referral_metadata,
                title="Client Service Request Manuscript"
            )

            if referral_metadata:
                self._add_metadata_section(story, referral_metadata)

            story.append(Spacer(1, 0.3 * inch))
            story.append(self._create_separator_line())
            story.append(Spacer(1, 0.3 * inch))
            self._add_service_request_section(story, request_details)

            story.append(Spacer(1, 0.5 * inch))
            story.append(self._create_separator_line())
            story.append(Spacer(1, 0.2 * inch))
            self._add_footer(story)

            doc.build(story, onFirstPage=self._add_page_number, onLaterPages=self._add_page_number)
            pdf_bytes = buffer.getvalue()
            buffer.close()
            logger.info(f"Successfully generated service request PDF with {len(request_details)} fields")
            return pdf_bytes
        except Exception as e:
            logger.error(f"Error generating service request PDF: {e}")
            raise
    
    def _add_header(
        self,
        story: List,
        metadata: Optional[Dict] = None,
        title: str = "Client-AI Conversation Transcript"
    ):
        """Add document header"""
        # Title
        title_para = Paragraph(title, self.styles['CustomTitle'])
        story.append(title_para)
        story.append(Spacer(1, 0.2 * inch))
        
        # Subtitle
        subtitle = Paragraph("LawNeeds Legal Consultation System", self.styles['CustomSubtitle'])
        story.append(subtitle)
        story.append(Spacer(1, 0.1 * inch))
        
        # Generation info
        generation_date = datetime.utcnow().strftime('%B %d, %Y at %H:%M UTC')
        gen_info = Paragraph(
            f"<i>Generated: {generation_date}</i>",
            self.styles['Metadata']
        )
        story.append(gen_info)
    
    def _add_metadata_section(self, story: List, metadata: Dict[str, Any]):
        """Add metadata section with referral details"""
        story.append(Spacer(1, 0.3 * inch))
        
        # Section title
        section_title = Paragraph("Case Information", self.styles['CustomSubtitle'])
        story.append(section_title)
        story.append(Spacer(1, 0.15 * inch))
        
        # Create metadata table
        data = []
        
        if metadata.get('client_name'):
            data.append(['Client Name:', metadata['client_name']])
        
        if metadata.get('referral_id'):
            data.append(['Referral ID:', str(metadata['referral_id'])])
        
        if metadata.get('legal_area'):
            data.append(['Legal Area:', metadata['legal_area']])
        
        if metadata.get('consultation_type'):
            data.append(['Consultation Type:', metadata['consultation_type']])
        
        if metadata.get('urgency_level'):
            urgency_display = metadata['urgency_level'].replace('_', ' ').title()
            data.append(['Urgency Level:', urgency_display])
        
        if metadata.get('date_started'):
            data.append(['Date Started:', metadata['date_started']])
        
        if data:
            table = Table(data, colWidths=[2 * inch, 4 * inch])
            table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TEXTCOLOR', (0, 0), (0, -1), self.PRIMARY_COLOR),
                ('TEXTCOLOR', (1, 0), (1, -1), black),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 12),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
    
    def _add_conversation_section(self, story: List, chat_history: List[Dict]):
        """Add the conversation transcript section"""
        story.append(Spacer(1, 0.3 * inch))
        
        # Section title
        section_title = Paragraph("Conversation Transcript", self.styles['CustomSubtitle'])
        story.append(section_title)
        story.append(Spacer(1, 0.15 * inch))
        
        if not chat_history:
            no_history = Paragraph(
                "<i>No conversation history available.</i>",
                self.styles['Metadata']
            )
            story.append(no_history)
            return
        
        # Add each message
        for idx, message in enumerate(chat_history):
            role = message.get('role', 'unknown')
            content = message.get('content', '')
            timestamp = message.get('timestamp', '')
            
            # Add timestamp
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    formatted_time = dt.strftime('%H:%M:%S')
                    time_para = Paragraph(
                        f"<i>{formatted_time}</i>",
                        self.styles['Timestamp']
                    )
                    story.append(time_para)
                except Exception:
                    pass
            
            # Add message based on role
            if role == 'user':
                # User message with label
                label = Paragraph(
                    f"<b><font color='{self.USER_COLOR}'>👤 Client:</font></b>",
                    self.styles['UserMessage']
                )
                story.append(label)
                
                # Escape and format content
                escaped_content = self._escape_html(content)
                message_para = Paragraph(
                    escaped_content,
                    self.styles['UserMessage']
                )
                story.append(message_para)
                
            elif role in ['bot', 'assistant']:
                # AI message with label
                label = Paragraph(
                    f"<b><font color='{self.AI_COLOR}'>🤖 AI Assistant:</font></b>",
                    self.styles['AIMessage']
                )
                story.append(label)
                
                # Escape and format content
                escaped_content = self._escape_html(content)
                message_para = Paragraph(
                    escaped_content,
                    self.styles['AIMessage']
                )
                story.append(message_para)
            
            # Add spacing between messages
            story.append(Spacer(1, 0.15 * inch))
            
            # Add page break for very long conversations (every 20 messages)
            if (idx + 1) % 20 == 0 and idx < len(chat_history) - 1:
                story.append(PageBreak())

    def _add_service_request_section(self, story: List, request_details: List[Dict[str, Any]]):
        """Add direct request-service selections without a chat transcript."""
        story.append(Spacer(1, 0.3 * inch))
        section_title = Paragraph("User Selections", self.styles['CustomSubtitle'])
        story.append(section_title)
        story.append(Spacer(1, 0.15 * inch))

        data = []
        for item in request_details:
            label = str(item.get("label") or "").strip()
            value = item.get("value")
            if not label or value is None or str(value).strip() == "":
                continue
            data.append([
                f"{self._escape_html(label)}:",
                self._escape_html(str(value).strip())
            ])

        if not data:
            story.append(Paragraph("<i>No service request selections available.</i>", self.styles['Metadata']))
            return

        table = Table(data, colWidths=[2 * inch, 4 * inch])
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('TEXTCOLOR', (0, 0), (0, -1), self.PRIMARY_COLOR),
            ('TEXTCOLOR', (1, 0), (1, -1), black),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(table)
    
    def _add_footer(self, story: List):
        """Add document footer"""
        footer_text = Paragraph(
            "<b>CONFIDENTIAL LEGAL DOCUMENT</b><br/>"
            "This transcript contains confidential client communications. "
            "Unauthorized distribution is prohibited.<br/>"
            "<i>Generated by LawNeeds Legal Platform</i>",
            self.styles['Footer']
        )
        story.append(footer_text)
    
    def _create_separator_line(self):
        """Create a horizontal separator line"""
        return Table(
            [['']], 
            colWidths=[6.5 * inch],
            style=TableStyle([
                ('LINEABOVE', (0, 0), (-1, 0), 1, self.LIGHT_GRAY),
            ])
        )
    
    def _add_page_number(self, canvas, doc):
        """Add page number to each page"""
        page_num = canvas.getPageNumber()
        text = f"Page {page_num}"
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(HexColor('#9CA3AF'))
        canvas.drawRightString(7.5 * inch, 0.5 * inch, text)
        canvas.restoreState()
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters for ReportLab"""
        if not text:
            return ""
        
        # Replace HTML special characters
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        
        # Convert newlines to breaks
        text = text.replace('\n', '<br/>')
        
        return text
