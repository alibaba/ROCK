// @ts-check
// `@type` JSDoc annotations allow editor autocompletion and type checking
// (when paired with `@ts-check`).
// There are various equivalent ways to declare your Docusaurus config.
// See: https://docusaurus.io/docs/api/docusaurus-config

import { themes as prismThemes } from 'prism-react-renderer';

function hiddenTargetSidebars(items) {
  // hidden not display sidebar
  const result = items.filter(item => {
    return !(item.type === 'doc' && HiddenSidebars.includes(item.id));
  }).map((item) => {
    if (item.type === 'category') {
      return { ...item, items: hiddenTargetSidebars(item.items) };
    }
    return item;
  });
  return result;
}
// 如果需要在sidebar隐藏的话，可以把文档id加到这里，id的命名方式为 路径名/文件名
const HiddenSidebars = ['Getting Started/quickstart', 'References/Python SDK References/python_sdk', 'Release Notes/index']

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'ROCK',
  tagline: 'Reinforcement Open Construction Kit',
  favicon: 'img/logo.png',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Set the production url of your site here
  url: 'https://alibaba.github.io',
  // Set the /<baseUrl>/ pathname under which your site is served
  // For GitHub pages deployment, it is often '/<projectName>/'
  baseUrl: '/ROCK/',

  // GitHub pages deployment config.
  // If you aren't using GitHub pages, you don't need these.
  organizationName: 'alibaba', // Usually your GitHub org/user name.
  projectName: 'ROCK', // Usually your repo name.

  onBrokenLinks: 'ignore',

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. For example, if your site is Chinese, you
  // may want to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-Hans'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          path: "rock",
          sidebarPath: './sidebars.js',
          sidebarCollapsed: false,
          // Please change this to your repo.
          // Remove this to remove the "edit this page" links.
          editUrl:
            'https://github.com/alibaba/ROCK/tree/master/docs/rock/',
          showLastUpdateTime: true,
          sidebarItemsGenerator: async ({ defaultSidebarItemsGenerator, ...args }) => {
            const sidebarItems = await defaultSidebarItemsGenerator(args);
            return hiddenTargetSidebars(sidebarItems);
          },
        },
        theme: {
          customCss: './src/css/custom.css',
        },
        gtag: {
          trackingID: 'G-26LMNFB70V',
          anonymizeIP: true,
        },
      }),
    ],
  ],

  themes: [
    [
      // @ts-ignore
      require.resolve("@easyops-cn/docusaurus-search-local"),
      /** @type {import("@easyops-cn/docusaurus-search-local").PluginOptions} */
      // @ts-ignore
      ({
        hashed: true,
        indexBlog: false,
        // For Docs usingChinese, it is recomended to set:
        language: ["en", 'zh'],
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      // Replace with your project's social card
      image: 'img/docusaurus-social-card.jpg',
      colorMode: {
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'ROCK',
        logo: {
          alt: 'ROCK',
          src: 'img/logo.png',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'tutorialSidebar',
            position: 'left',
            label: 'Docs',
          },
          {
            type: 'localeDropdown',
            position: 'right',
          },
          {
            href: "https://deepwiki.com/alibaba/ROCK",
            label: "DeepWiki",
            position: 'right',
          },
          {
            href: 'https://github.com/alibaba/ROCK',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              {
                label: 'Overview',
                to: '/docs/overview',
              },
              {
                label: 'Quick Start',
                to: '/docs/Getting%20Started/quickstart',
              },
            ],
          },
          {
            title: 'Community',
            items: [
              {
                label: 'Stack Overflow',
                href: 'https://stackoverflow.com/questions/tagged/docusaurus',
              }
            ],
          },
          {
            title: 'More',
            items: [
              {
                label: 'GitHub',
                href: 'https://github.com/alibaba/ROCK',
              },
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} Alibaba.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
      },
    }),
};

export default config;
