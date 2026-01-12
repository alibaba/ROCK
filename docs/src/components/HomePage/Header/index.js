import React from 'react';
import { useThemeConfig } from '@docusaurus/theme-common';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Translate from '@docusaurus/Translate';

import styles from './styles.module.css';

const Header = ({ locale }) => {
  const {
    navbar: { logo },
  } = useThemeConfig();
  const isChinese = locale !== 'en';
  console.log('locale', locale, isChinese);

  const toggleLanguage = () => {
    handleNavigation(isChinese ? '/ROCK/' : '/ROCK/zh-Hans/');
  };

  const handleNavigation = (url) => {
    if (url.startsWith('http')) {
      window.open(url, '_blank');
    } else {
      window.location.href = url;
    }
  };

  return (
    <header className={styles.header}>
      <div className={styles.container}>
        {/* Logo和文案区域 */}
        <div className={styles.logoSection}>
          <div className={styles.logo}>
            <div className={styles.logoIcon}>
              <img src={useBaseUrl(logo?.src)} alt="logo" />
            </div>
          </div>
          <div className={styles.textSection}>
            <div className={styles.primaryText}>ROCK</div>
            <div className={styles.secondaryText}>Reinforcement Open Construction Kit</div>
          </div>
        </div>

        {/* 导航按钮区域 */}
        <div className={styles.navSection}>
          <button
            className={styles.navButton}
            onClick={() => handleNavigation(!isChinese ? '/ROCK/' : '/ROCK/zh-Hans/')}
          >
            <Translate>
              Home
            </Translate>
          </button>
          <button
            className={styles.navButton}
            onClick={() => handleNavigation(!isChinese ? '/ROCK/docs/overview/' : '/ROCK/zh-Hans/docs/overview/')}
          >
            <Translate>
              Docs
            </Translate>
          </button>
          <button
            className={styles.navButton}
            onClick={() => handleNavigation('https://github.com/alibaba/ROCK')}
          >
            GitHub
          </button>
          <button
            className={styles.languageButton}
            onClick={toggleLanguage}
          >
            {isChinese ? '中文' : 'En'}
          </button>
        </div>
      </div>
    </header>
  );
};

export default Header;
