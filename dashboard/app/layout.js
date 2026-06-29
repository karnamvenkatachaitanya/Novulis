import "./globals.css";

export const metadata = {
  title: "WaiverPro QA Compliance Agent Control Dashboard",
  description: "Real-time visualization and execution of the WaiverPro compliance agent.",
  icons: {
    icon: [],
  },
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        {children}
      </body>
    </html>
  );
}
