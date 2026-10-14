def html(otp):
    return f"""
        <html>
        <head>
            <meta charset="utf-8" />
            <style>
                body {{ font-family: Arial, sans-serif; color: #1d1d1d; margin:0; padding:0 40px; background:#F0F5F2; }}
                .container {{ max-width:600px; margin:20px auto; background:#ffffff; border-radius:15px; overflow:hidden; }}
                .band {{ background:#033126; padding:50px 24px; text-align:center; }}
                .band h1 {{ margin:0; color:#ffffff; font-size:26px; line-height:1; }}
                .band .motto {{ margin:6px 0 0; color:#DD8550; font-size:13px; font-weight:600; letter-spacing:0.3px; }}
                .content {{ padding:28px 24px; }}
                .content p {{ line-height:1.5; font-size:15px; }}
                .welcome {{ background:#033126; color:#ffffff; padding:20px; border-radius:6px; text-align:center; }}
                .welcome h3 {{ margin:0 0 6px 0; }}
                .welcome p {{ margin:0; }}
                .callout {{ background:#F0F5F2; padding:16px 20px; border-radius:4px; border-left:4px solid #DD8550; margin-top:20px; }}
                .otp-code {{ display:inline-block; background-color:#ffffff; color:#1d1d1d;
                            padding:15px 25px; font-size:24px; font-weight:bold;
                            border-radius:8px; letter-spacing:3px; text-align:center;
                            margin:16px 0; border:2px dashed #DD8550; }}
                .warning {{ color:#c62828; font-weight:bold; }}
                .features {{ margin-top:16px; padding-left:20px; }}
                .features li {{ margin-bottom:8px; line-height:1.4; }}
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
                    <div class="welcome">
                        <h3>Welcome to LawNeeds</h3>
                        <p>You're just one step away from accessing our platform</p>
                    </div>

                    <p style="margin-top:20px;">Thank you for registering with LawNeeds! To complete your account setup and gain access to our platform, please verify your email address.</p>

                    <div class="callout">
                        <p style="margin:0 0 8px 0;"><strong>Your email verification code is:</strong></p>
                        <div style="text-align:center;">
                            <div class="otp-code">{otp}</div>
                        </div>
                    </div>

                    <p style="margin-top:20px;">To complete your registration:</p>
                    <ol>
                        <li>Return to the registration page</li>
                        <li>Enter this verification code when prompted</li>
                        <li>Complete your account setup</li>
                        <li>Start exploring LawNeeds!</li>
                    </ol>

                    <p class="warning">Important:</p>
                    <ul>
                        <li>This code will expire in 15 minutes for security reasons</li>
                        <li>Do not share this code with anyone</li>
                        <li>If you didn't create this account, please ignore this email</li>
                    </ul>

                    <p>Once verified, you'll have full access to:</p>
                    <ul class="features">
                        <li>Legal knowledge bases and resources</li>
                        <li>AI-powered legal chat assistance</li>
                        <li>Professional legal services directory</li>
                        <li>Profile management and networking tools</li>
                    </ul>

                    <p style="margin-top:20px;">Welcome aboard!<br/>The LawNeeds Team</p>
                </div>

                <div class="footer">
                    <p class="tagline">Legal Support Made Simple</p>
                    <p>Website: <a href="https://lawneeds.co.uk">lawneeds.co.uk</a></p>
                    <p>Support: <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                    <p><a href="https://lawneeds.co.uk/privacy-policy-for-application/">Privacy Policy</a> | <a href="https://lawneeds.co.uk/term-and-conditions-users/">Terms of Use</a></p>
                    <p style="font-size:11px; color:#B9B9B9;">This is an automated message, please do not reply to this email.</p>
                    <p class="copyright">&copy; 2026 LawNeeds Ltd. All rights reserved.</p>
                </div>
            </div>
        </body>
        </html>
        """


def text(otp):
    return f"""
        Welcome to LawNeeds!

        Thank you for registering with LawNeeds! To complete your account setup and
        gain access to our platform, please verify your email address.

        Your email verification code is: {otp}

        To complete your registration:
        1. Return to the registration page
        2. Enter this verification code when prompted
        3. Complete your account setup
        4. Start exploring LawNeeds!

        IMPORTANT:
        - This code will expire in 15 minutes for security reasons
        - Do not share this code with anyone
        - If you didn't create this account, please ignore this email

        Once verified, you'll have full access to:
        - Legal knowledge bases and resources
        - AI-powered legal chat assistance
        - Professional legal services directory
        - Profile management and networking tools

        Welcome aboard!
        The LawNeeds Team

        LawNeeds Ltd
        Legal Support Made Simple
        Website: https://lawneeds.co.uk
        Support: support@lawneeds.co.uk

        Privacy Policy: https://lawneeds.co.uk/privacy-policy-for-application/
        Terms of Use: https://lawneeds.co.uk/term-and-conditions-users/
        """