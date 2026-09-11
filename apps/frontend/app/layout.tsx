import type { Metadata } from 'next';
import './(default)/css/globals.css';

// NOTE: Google Fonts (next/font/google) has been removed to keep the app
// working offline / behind the GFW (fonts.gstatic.com is unreachable in
// mainland China). The font CSS variables (--font-geist, --font-noto-sans-sc,
// etc.) are now defined in globals.css :root with system font stacks. To
// restore the original Google Fonts, revert this file and uncomment the
// next/font/google imports in the original layout.tsx.

export const metadata: Metadata = {
  title: 'Resume Matcher',
  description: 'Build your resume with Resume Matcher',
  applicationName: 'Resume Matcher',
  keywords: ['resume', 'matcher', 'job', 'application'],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-US" className="h-full" suppressHydrationWarning>
      <body
        className="antialiased bg-background text-ink-soft min-h-full"
      >
        {children}
      </body>
    </html>
  );
}
