import { spawn } from "child_process";

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const pathsStr = searchParams.get("paths") || "";
  const paths = pathsStr.split(",").filter((p) => p.trim());

  // Prepare standard arguments targeting main.py in parent directory
  const args = ["main.py", "--no-email", "--no-github-issues", "--verbose"];
  
  if (paths.length > 0) {
    paths.forEach((p) => {
      args.push("--target-path");
      args.push(p);
    });
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      let closed = false;
      const pythonCmd = process.platform === "win32" ? "python" : "python3";

      // Spawn Python process with unbuffered IO to enable real-time logs streaming
      const child = spawn(pythonCmd, args, {
        cwd: "../",
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
      });

      child.stdout.on("data", (data) => {
        if (closed) return;
        const lines = data.toString().split("\n");
        lines.forEach((line) => {
          if (line.trim()) {
            try {
              controller.enqueue(
                encoder.encode(`data: ${JSON.stringify({ type: "log", data: line })}\n\n`)
              );
            } catch (e) {}
          }
        });
      });

      child.stderr.on("data", (data) => {
        if (closed) return;
        const lines = data.toString().split("\n");
        lines.forEach((line) => {
          if (line.trim()) {
            try {
              controller.enqueue(
                encoder.encode(`data: ${JSON.stringify({ type: "log", data: line })}\n\n`)
              );
            } catch (e) {}
          }
        });
      });

      child.on("close", (code) => {
        if (closed) return;
        closed = true;
        try {
          controller.enqueue(
            encoder.encode(`data: ${JSON.stringify({ type: "done", code })}\n\n`)
          );
          controller.close();
        } catch (e) {}
      });

      child.on("error", (err) => {
        if (closed) return;
        closed = true;
        try {
          controller.enqueue(
            encoder.encode(`data: ${JSON.stringify({ type: "error", error: err.message })}\n\n`)
          );
          controller.close();
        } catch (e) {}
      });
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
    },
  });
}
