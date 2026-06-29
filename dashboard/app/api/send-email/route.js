import nodemailer from "nodemailer";
import { promises as fs } from "fs";
import path from "path";

// Helper to parse environmental configurations directly from parent workspace .env
async function loadEnv() {
  try {
    const envPath = path.resolve("../.env");
    const content = await fs.readFile(envPath, "utf-8");
    const env = {};
    content.split(/\r?\n/).forEach((line) => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) return;
      const match = trimmed.match(/^([\w.-]+)\s*=\s*(.*)$/);
      if (match) {
        let val = match[2].trim();
        // Unwrap quotes if present
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
          val = val.slice(1, -1);
        }
        env[match[1]] = val;
      }
    });
    return env;
  } catch (err) {
    console.error("Could not read parent .env file, fallback to process.env", err);
    return process.env;
  }
}

export async function POST(request) {
  try {
    const { runId, email } = await request.json();
    const env = await loadEnv();

    const smtpHost = env.SMTP_HOST;
    const smtpPort = parseInt(env.SMTP_PORT || "587");
    const smtpUser = env.SMTP_USERNAME;
    const smtpPass = env.SMTP_PASSWORD;
    const alertFrom = env.ALERT_FROM;
    const alertTo = email?.trim() || env.ALERT_TO;

    if (!alertTo) {
      return Response.json(
        { success: false, error: "Recipient email address is missing. Please enter a valid email address in the input field." },
        { status: 400 }
      );
    }

    if (!smtpHost || !smtpUser || !smtpPass) {
      return Response.json(
        { success: false, error: "SMTP settings (host/user/pass) are missing in parent .env configuration." },
        { status: 400 }
      );
    }

    const reportsDir = path.resolve("../reports");
    let pdfName = "";

    if (runId) {
      pdfName = `compliance-report-${runId}.pdf`;
    } else {
      const files = await fs.readdir(reportsDir);
      const pdfFiles = files
        .filter((f) => f.startsWith("compliance-report-") && f.endsWith(".pdf"))
        .sort()
        .reverse();
      if (pdfFiles.length === 0) {
        return Response.json({ success: false, error: "No compiled PDF reports found inside reports directory." }, { status: 404 });
      }
      pdfName = pdfFiles[0];
    }

    const pdfPath = path.join(reportsDir, pdfName);
    const pdfData = await fs.readFile(pdfPath);

    // Create secure transporter configuration
    const transporter = nodemailer.createTransport({
      host: smtpHost,
      port: smtpPort,
      secure: smtpPort === 465, // true for port 465, false for starttls
      auth: {
        user: smtpUser,
        pass: smtpPass,
      },
      tls: {
        // Only bypass certificate verification in development environments
        rejectUnauthorized: process.env.NODE_ENV !== 'development',
      },
    });

    const mailOptions = {
      from: alertFrom,
      to: alertTo,
      subject: `WaiverPro Compliance Report - Run ${pdfName.replace("compliance-report-", "").replace(".pdf", "")}`,
      text: `Stakeholders,\n\nPlease find attached the automated visual compliance audit report PDF generated for WaiverPro.`,
      html: `
        <div style="font-family: Arial, sans-serif; padding: 20px; color: #1F2937;">
          <h2 style="color: #1A365D;">WaiverPro QA Compliance Audit Notification</h2>
          <p>The latest compliance sweep was executed successfully.</p>
          <p><strong>Report File attached:</strong> <code>${pdfName}</code></p>
          <hr style="border: 0; border-top: 1px solid #E5E7EB; margin: 20px 0;">
          <p style="font-size: 0.85rem; color: #6B7280;">WaiverPro Compliance Automation System &copy; 2026</p>
        </div>
      `,
      attachments: [
        {
          filename: pdfName,
          content: pdfData,
          contentType: "application/pdf",
        },
      ],
    };

    const info = await transporter.sendMail(mailOptions);
    return Response.json({ success: true, messageId: info.messageId, recipient: alertTo });
  } catch (err) {
    return Response.json({ success: false, error: err.message }, { status: 500 });
  }
}
