import { promises as fs } from "fs";
import path from "path";

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const filePath = searchParams.get("path");
  if (!filePath) {
    return new Response("Missing path parameter", { status: 400 });
  }

  // Security constraint: only allow files inside parent directory
  const resolved = path.resolve("../", filePath);
  const parentResolved = path.resolve("../");
  
  if (!resolved.startsWith(parentResolved)) {
    return new Response("Access Denied: Path outside of workspace root", { status: 403 });
  }

  try {
    const data = await fs.readFile(resolved);
    let contentType = "application/octet-stream";
    
    if (resolved.toLowerCase().endsWith(".png")) {
      contentType = "image/png";
    } else if (resolved.toLowerCase().endsWith(".pdf")) {
      contentType = "application/pdf";
    } else if (resolved.toLowerCase().endsWith(".json")) {
      contentType = "application/json";
    } else if (resolved.toLowerCase().endsWith(".html")) {
      contentType = "text/html";
    }

    return new Response(data, {
      headers: {
        "Content-Type": contentType,
      },
    });
  } catch (err) {
    return new Response(`File not found: ${err.message}`, { status: 404 });
  }
}
