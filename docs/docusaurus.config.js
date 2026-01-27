// @ts-check
// `@type` JSDoc annotations allow editor autocompletion and type checking
// (when paired with `@ts-check`).
// There are various equivalent ways to declare your Docusaurus config.
// See: https://docusaurus.io/docs/api/docusaurus-config

import { themes as prismThemes } from 'prism-react-renderer';
import versions from './versions.json';

// 添加版本数组到对象的转换函数
/**
 * @param {string[]} versionsArray
 * @returns {Record<string, {label: string, banner: string}>}
 */
function convertVersionsArrayToObject(versionsArray) {
  /** @type {Record<string, {label: string, banner: string}>} */
  const versionsObject = {};
  versionsArray.forEach(/** @type {string} */(version) => {
    versionsObject[version] = {
      label: version,
      banner: 'none'
    };
  });
  return versionsObject;
}

/**
 * @param {Array<{type: string, id: string}>} items
 * @returns {Array<{type: string, id: string}>}
 */
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

/**
 * @param {Array<any>} items
 * @returns {Array<any>}
 */
const reverseReleaseNoteSidebars = (items) => {
  // 筛选出符合 Release Notes 格式的项
  const releaseNoteItems = items.filter(item => item.type !== 'category').filter(item => {
    const ids = item.id?.split('/');
    // 检查是否包含'Release Notes'且至少有2个部分
    if (ids.includes('Release Notes') && ids.length >= 2) {
      // 检查最后一个部分是否符合版本号格式(x.x.x 或 x.x.x.x)
      const versionPattern = /^v?\d+\.\d+\.\d+(\.\d+)?$/;
      return versionPattern.test(ids[ids.length - 1]);
    }
    return false;
  });

  // 对符合条件的Release Notes条目进行排序
  if (releaseNoteItems.length > 0) {
    // 按版本号从大到小排序
    releaseNoteItems.sort((a, b) => {
      const versionA = a.id.split('/').pop();
      const versionB = b.id.split('/').pop();

      // 将版本号分割为数字数组
      const partsA = versionA.replace(/^v/, '').split('.').map(Number);
      const partsB = versionB.replace(/^v/, '').split('.').map(Number);

      // 逐个比较版本号的每个部分
      for (let i = 0; i < Math.max(partsA.length, partsB.length); i++) {
        const partA = partsA[i] || 0;
        const partB = partsB[i] || 0;

        if (partA > partB) return -1;
        if (partA < partB) return 1;
      }

      return 0;
    });
  }

  // 合并非Release Notes项目和已排序的Release Notes项目
  const nonReleaseNoteItems = items.filter(item => !releaseNoteItems.includes(item));
  return [...nonReleaseNoteItems, ...releaseNoteItems];
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
            'https://github.com/alibaba/ROCK/tree/master/docs/',
          showLastUpdateTime: true,
          sidebarItemsGenerator: async ({ defaultSidebarItemsGenerator, ...args }) => {
            const sidebarItems = await defaultSidebarItemsGenerator(args);
            // 去掉需要隐藏的侧边导航
            const filterHiddenSidebars = hiddenTargetSidebars(sidebarItems);
            // release note按照版本号倒排
            return reverseReleaseNoteSidebars(filterHiddenSidebars);
          },
          lastVersion: '1.1.x',
          includeCurrentVersion: false,
          versions: convertVersionsArrayToObject(versions)
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
            type: 'docsVersionDropdown',
            position: "right",
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