import { promises as fs } from "fs";
import path from "path";

export async function GET() {
  try {
    const reportsDir = path.resolve("../reports");
    
    // Ensure the folder exists
    try {
      await fs.mkdir(reportsDir, { recursive: true });
    } catch (_) {}

    const files = await fs.readdir(reportsDir);

    // Filter to find latest summary-*.json file
    const summaryFiles = files
      .filter((f) => f.startsWith("summary-") && f.endsWith(".json"))
      .sort()
      .reverse(); // Latest timestamp first

    if (summaryFiles.length === 0) {
      return Response.json({ success: false, message: "No scan summaries found yet" });
    }

    const latestSummaryName = summaryFiles[0];
    const summaryContent = await fs.readFile(path.join(reportsDir, latestSummaryName), "utf-8");
    const summary = JSON.parse(summaryContent);

    // Check if the corresponding compiled PDF report exists
    const pdfName = `compliance-report-${summary.run_id}.pdf`;
    const pdfPath = path.join("reports", pdfName);
    const pdfExists = files.includes(pdfName);

    return Response.json({
      success: true,
      runId: summary.run_id,
      reports: summary.reports,
      pdfPath: pdfExists ? pdfPath : null,
    });
  } catch (err) {
    return Response.json({ success: false, error: err.message }, { status: 500 });
  }
}
