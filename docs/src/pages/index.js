import clsx from 'clsx';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import HomePage from '@site/src/components/HomePage';

export default function Home() {
  const { siteConfig, i18n } = useDocusaurusContext();
  const { currentLocale } = i18n;

  return (
    <Layout
      title={`Hello from ${siteConfig.title}`}
      description="Description will go into a meta tag in <head />">
      <main>
        <HomePage currentLocale={currentLocale} />
      </main>
    </Layout>
  );
}
