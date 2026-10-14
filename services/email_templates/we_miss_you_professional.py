def html(display_name):
    return f"""
        <html>
        <head>
            <meta charset="utf-8" />
            <style>
                body {{ font-family: Arial, sans-serif; color: #1d1d1d; margin:0; padding:0 40px; background:#F0F5F2; }}
                .container {{ max-width:600px; margin:20px auto; background:#ffffff; border-radius:15px; overflow:hidden; }}
                .band {{ background:#033126; padding:44px 24px; text-align:center; }}
                .band h1 {{ margin:0; color:#ffffff; font-size:26px; line-height:1; }}
                .band .motto {{ margin:6px 0 0; color:#DD8550; font-size:13px; font-weight:600; letter-spacing:0.3px; }}
                .content {{ padding:28px 24px; }}
                .content p {{ line-height:1.5; font-size:15px; }}
                .features {{ margin-top:16px; padding-left:20px; }}
                .features li {{ margin-bottom:8px; line-height:1.4; }}
                .callout {{ background:#F0F5F2; padding:16px 20px; border-radius:4px; border-left:4px solid #DD8550; margin-top:20px; }}
                .footer {{ background:#033126; padding:24px; text-align:center; }}
                .footer p {{ margin:10px 0; color:#ffffff; font-size:13px; }}
                .footer .tagline {{ font-weight:bold; font-size:15px; }}
                .footer a {{ color:#ffffff; text-decoration:underline; }}
                .copyright {{ margin-top:6px; font-size:11px; color:#B9B9B9; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="band">
                    <table role="presentation" class="band-inner" align="center" cellpadding="0" cellspacing="0" style="margin:0 auto;">
                        <tr>
                            <td valign="middle" align="right" style="padding:0 6px 4px 0; vertical-align:middle; line-height:0;">
                                <img src="https://lawneeds-official-db.s3.eu-west-2.amazonaws.com/email-assets/adaptive-icon.png" alt="LawNeeds" width="50" height="50" style="width:50px; height:50px; border-radius:8px; display:block; vertical-align:middle;" />
                            </td>
                            <td valign="middle" align="left" style="padding:0; vertical-align:middle;">
                                <h1 style="margin:0; color:#ffffff; font-size:26px; line-height:1;">LawNeeds</h1>
                                <p style="margin:6px 0 0; color:#DD8550; font-size:13px; font-weight:600; letter-spacing:0.3px;">Legal Support Made Simple</p>
                            </td>
                        </tr>
                    </table>
                </div>

                <div class="content">
                    <p>Hi <strong>{display_name}</strong>,</p>

                    <p>It's been a little while since you last used LawNeeds, and we wanted to check in.</p>

                    <p>Your professional account gives you another way to connect with individuals actively looking for legal support.</p>

                    <p>With LawNeeds, you can:</p>
                    <ul class="features">
                        <li>Receive qualified enquiries and referral opportunities</li>
                        <li>Connect with users seeking professional legal assistance</li>
                        <li>Offer consultations directly through the platform</li>
                        <li>Manage your consultation availability</li>
                        <li>Communicate securely with prospective and existing clients</li>
                        <li>Maintain your professional profile and visibility on LawNeeds</li>
                    </ul>

                    <div class="callout">
                        <p style="margin:0;">Log back into the LawNeeds app to check your account, update your availability and ensure your professional profile is up to date.</p>
                    </div>

                    <p style="margin-top:20px;">We look forward to welcoming you back.</p>

                    <p>Kind regards,<br/>The LawNeeds Team</p>
                </div>

                <div class="footer">
                    <p class="tagline">Legal Support Made Simple</p>
                    <p>Website: <a href="https://lawneeds.co.uk">lawneeds.co.uk</a></p>
                    <p>Support: <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                    <p><a href="https://lawneeds.co.uk/privacy-policy-for-application/">Privacy Policy</a> | <a href="https://lawneeds.co.uk/term-and-conditions-users/">Terms of Use</a></p>
                    <p style="font-size:11px; color:#B9B9B9;">You're receiving this email because you created a LawNeeds account. Manage notifications or unsubscribe in your account settings.</p>
                    <p class="copyright">&copy; 2026 LawNeeds Ltd. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """


def text(display_name):
    return f"""
        Hi {display_name},

        It's been a little while since you last used LawNeeds, and we wanted to check in.

        Your professional account gives you another way to connect with individuals actively looking for legal support.

        With LawNeeds, you can:
        - Receive qualified enquiries and referral opportunities
        - Connect with users seeking professional legal assistance
        - Offer consultations directly through the platform
        - Manage your consultation availability
        - Communicate securely with prospective and existing clients
        - Maintain your professional profile and visibility on LawNeeds

        Log back into the LawNeeds app to check your account, update your availability and ensure your professional profile is up to date.

        We look forward to welcoming you back.

        Kind regards,
        The LawNeeds Team

        LawNeeds Ltd
        Legal Support Made Simple
        Website: https://lawneeds.co.uk
        Support: support@lawneeds.co.uk

        Privacy Policy: https://lawneeds.co.uk/privacy-policy-for-application/
        Terms of Use: https://lawneeds.co.uk/term-and-conditions-users/

        You're receiving this email because you created a LawNeeds account.
        """