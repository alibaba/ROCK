import { useEffect, useState } from 'react';

export default function useIsDark() {
  const [isDark, setIsDark] = useState(false);

  useEffect(() => {
    const update = () => {
      const html = document.documentElement;
      const classVal = html.getAttribute('data-theme') || '';
      setIsDark(classVal === 'dark');
    };
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);

  return isDark;
}
