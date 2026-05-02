import "./globals.css";

export const metadata = {
  title: "BoardBaby",
  description: "Board material summarization for exec and LP updates"
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
