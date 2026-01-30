import React, { useState, useEffect, useRef } from 'react';
import { Button, Image, Modal, ConfigProvider, theme } from 'antd';
import { GithubOutlined, WechatOutlined, XOutlined } from '@ant-design/icons';
import clsx from 'clsx';
import dayjs from 'dayjs';
import CountUp from 'react-countup';
import Translate from '@docusaurus/Translate';
import { gsap } from 'gsap';
import Header from './Header';

import styles from './styles.module.css';

export default ({ currentLocale }) => {
  const [open, setOpen] = useState(false);
  const aboutRef = useRef(null);
  const featuresRef = useRef(null);
  const opensourceRef = useRef(null);
  const rockRollRef = useRef(null);
  const communityRef = useRef(null);
  const [todayStat, setTodayStat] = useState({});
  const [startCounting, setStartCounting] = useState(false);
  const today = dayjs().format('YYYY-MM-DD');
  const isChinese = currentLocale !== 'en';

  useEffect(() => {
    const observerOptions = {
      root: null,
      rootMargin: '0px',
      threshold: 0.2
    };

    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add(styles.visible);
          // 当开源社区统计区域可见时，开始计数
          if (entry.target === opensourceRef.current) {
            setStartCounting(true);
          }
        }
      });
    }, observerOptions);

    const sections = [
      aboutRef.current,
      featuresRef.current,
      opensourceRef.current,
      rockRollRef.current,
      communityRef.current
    ];

    sections.forEach(section => {
      if (section) {
        observer.observe(section);
      }
    });

    return () => {
      sections.forEach(section => {
        if (section) {
          observer.unobserve(section);
        }
      });
    };
  }, []);

  useEffect(() => {
    const childObserverOptions = {
      root: null,
      rootMargin: '0px',
      threshold: 0.1
    };

    const childObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const index = parseInt(entry.target.dataset.index);
          entry.target.style.transitionDelay = `${index * 0.15}s`;
          entry.target.classList.add(styles.childVisible);
        }
      });
    }, childObserverOptions);

    // 监听.images模块的子元素
    const aboutImages = aboutRef.current?.querySelectorAll(`.${styles.images} .${styles.sub}`);
    aboutImages?.forEach((element, index) => {
      element.dataset.index = index;
      childObserver.observe(element);
    });

    // 监听.features模块的子元素
    const featuresCards = featuresRef.current?.querySelectorAll(`.${styles.card}`);
    featuresCards?.forEach((element, index) => {
      element.dataset.index = index;
      childObserver.observe(element);
    });

    // 监听.opensource模块的统计项
    const opensourceStats = opensourceRef.current?.querySelectorAll(`.${styles.statItem}`);
    opensourceStats?.forEach((element, index) => {
      element.dataset.index = index;
      childObserver.observe(element);
    });

    // 监听.community模块的子元素
    const communityElements = communityRef.current?.querySelectorAll(`.${styles.content} > div`);
    communityElements?.forEach((element, index) => {
      element.dataset.index = index;
      childObserver.observe(element);
    });

    // 监听.rock模块的子元素
    const rockElements = document.querySelector(`.${styles.rock}`)?.children;
    Array.from(rockElements || []).forEach((element, index) => {
      element.dataset.index = index;
      childObserver.observe(element);
    });

    return () => {
      aboutImages?.forEach(element => childObserver.unobserve(element));
      featuresCards?.forEach(element => childObserver.unobserve(element));
      opensourceStats?.forEach(element => childObserver.unobserve(element));
      communityElements?.forEach(element => childObserver.unobserve(element));
      Array.from(rockElements || []).forEach(element => childObserver.unobserve(element));
    };
  }, []);

  useEffect(() => {
    fetch('/ROCK/stats.json').then(res => res.json()).then(data => {
      setTodayStat(data[today]);
    })
  }, []);

  // 鼠标跟随边框高亮效果
  useEffect(() => {
    const featuresSection = featuresRef.current;
    if (!featuresSection) return;

    const cards = featuresSection.querySelectorAll(`.${styles.card}`);

    const handleMouseMove = (e) => {
      const rect = featuresSection.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;

      cards.forEach((card) => {
        const cardRect = card.getBoundingClientRect();
        const cardX = cardRect.left - rect.left;
        const cardY = cardRect.top - rect.top;

        // 计算鼠标相对于卡片的位置
        const relativeX = mouseX - cardX;
        const relativeY = mouseY - cardY;

        // 使用 gsap 动画更新 CSS 变量
        gsap.to(card, {
          '--mouse-x': `${relativeX}px`,
          '--mouse-y': `${relativeY}px`,
          duration: 0.2,
          ease: 'power2.out'
        });
      });
    };

    featuresSection.addEventListener('mousemove', handleMouseMove);

    return () => {
      featuresSection.removeEventListener('mousemove', handleMouseMove);
    };
  }, []);

  return <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>
    <div className={styles.container} id="home">
      <div className={styles.header}>
        <video
          className={styles.headerVideo}
          autoPlay
          muted
          loop
          playsInline
          poster="https://img.alicdn.com/imgextra/i1/6000000003421/O1CN012ghG7A1b8s3dGSHNy_!!6000000003421-0-tbvideo.jpg"
        >
          <source
            src="https://cloud.video.taobao.com/vod/PHpu7ZebVDB7krFCIj3w-vkcDhcDGSnzZ97XDz8zBOE.mp4"
            type="video/mp4"
          />
        </video>
        <div className={styles.headerOverlay}></div>

        <Header locale={currentLocale} />

        <div className={styles.rock}>
          <Image src="https://img.alicdn.com/imgextra/i2/O1CN01Qs91lg1oWljEJIOrF_!!6000000005233-55-tps-378-112.svg" preview={false}></Image>
          <div className={styles.description}>
            Reinforcement Learning Infrastructure Platform
          </div>
          <div className={styles.btns}>
            <Button className={styles.button} href={isChinese ? '/ROCK/zh-Hans/docs/overview' : '/ROCK/docs/overview'} target='_blank'>
              <Translate>
                Get Started
              </Translate>
            </Button>
          </div>
        </div>
      </div>
      <div ref={aboutRef} className={clsx(styles.about, styles.fadeInSection)}>
        <div className={styles.title}>
          <Translate>
            ABOUT ROCK
          </Translate>
        </div>
        <div className={styles.desc}>
          <Translate>
            ROCK (Reinforcement Open Construction Kit) is
          </Translate>
          &nbsp;<span className={styles.pot}>
            <Translate>
              an AI-powered reinforcement learning environment platform
            </Translate>
          </span>&nbsp;
          <Translate>
            that offers standardized infrastructure, intelligent model services, TPP access, and a rich set of built-in scenarios, enabling developers to rapidly conduct RL training tasks.
          </Translate>
        </div>
        <div className={styles.images}>
          <div className={styles.sub}>
            <div className={styles.imgTitle}>
              <Translate>
                AI Intelligence
              </Translate>
            </div>
            <Image src="https://img.alicdn.com/imgextra/i2/O1CN01RjzzjV1kvGRrbl81Q_!!6000000004745-2-tps-300-300.png" width={150} preview={false}></Image>
          </div>
          <div className={styles.sub}>
            <div className={styles.imgTitle}>
              <Translate>
                Cloud Native
              </Translate>
            </div>
            <Image src="https://img.alicdn.com/imgextra/i1/O1CN012BFZGj1lBIJKsyGjj_!!6000000004780-2-tps-300-300.png" width={150} preview={false}></Image>
          </div>
          <div className={styles.sub}>
            <div className={styles.imgTitle}>
              <Translate>
                Standardization
              </Translate>
            </div>
            <Image src="https://img.alicdn.com/imgextra/i4/O1CN01YKYHMl1bBA2PbxYjt_!!6000000003426-2-tps-300-300.png" width={150} preview={false}></Image>
          </div>
        </div>
      </div>
      <div
        ref={featuresRef}
        className={clsx(styles.features, styles.fadeInSection)}
        style={{ position: 'relative' }}
      >
        <div className={styles.title}>
          <Translate>
            CORE FEATURES
          </Translate>
        </div>
        <div className={styles.cards} style={{ gap: '30px' }}>
          <div
            className={clsx(styles.card, styles.multi)}
          >
            <Image src="https://img.alicdn.com/imgextra/i4/O1CN01535hV51bWgeY1WpjL_!!6000000003473-2-tps-1312-780.png" preview={false}></Image>
            <div className={styles.subTitle}>
              <Translate>
                Multi-Protocol Action Support
              </Translate>
            </div>
            <div className={styles.line}></div>
            <div className={styles.subDesc}>
              <Translate>
                Supports multiple action protocols including GEM, Bash, and Chat.
              </Translate>
            </div>
          </div>
          <div
            className={clsx(styles.card, styles.sandbox)}
          >
            <Image src="https://img.alicdn.com/imgextra/i1/O1CN0148oZxD1JlC7B4iKf4_!!6000000001068-2-tps-852-780.png" preview={false} ></Image>
            <div className={styles.subTitle}>
              <Translate>
                Sandbox Runtime
              </Translate>
            </div>
            <div className={styles.line}></div>
            <div className={styles.subDesc}>
              <Translate>
                Stateful runtime environments with multiple isolation mechanisms to ensure consistency and security
              </Translate>
            </div>
          </div>
        </div>
        <div className={styles.cards} style={{ marginTop: 30 }}>
          <div className={styles.imageCards}>
            <div
              className={clsx(styles.card, styles.deployment)}
            >
              <Image src="https://img.alicdn.com/imgextra/i1/O1CN01rNsOHc1Jc2C3UuYoh_!!6000000001048-2-tps-562-568.png" height={280} preview={false}></Image>
              <div className={styles.subTitle}>
                <Translate>
                  Flexible Deployment
                </Translate>
              </div>
              <div className={styles.line}></div>
              <div className={styles.subDesc}>
                <Translate>
                  Supports different deployment methods for diverse environment and OS
                </Translate>
              </div>
            </div>
            <div
              className={clsx(styles.card, styles.sdk)}
            >
              <Image src="https://img.alicdn.com/imgextra/i3/O1CN013dl5sG1HHJ2LEAew5_!!6000000000732-2-tps-562-568.png" height={280} preview={false}></Image>
              <div className={styles.subTitle}>
                <Translate>
                  Unified SDK Interface
                </Translate>
              </div>
              <div className={styles.line}></div>
              <div className={styles.subDesc}>
                <Translate>
                  Clean Python SDK for Env and Sandbox interaction
                </Translate>
              </div>
            </div>
          </div>
          <div className={styles.textCards}>
            <div
              className={clsx(styles.card, styles.arch)}
            >
              <div className={styles.subTitle}>
                <Translate>
                  Layered Service Architecture
                </Translate>
              </div>
              <div className={styles.line}></div>
              <div className={styles.subDesc}>
                <Translate>
                  Distributed Admin, Worker, and Rocklet architecture for scalable resource management
                </Translate>
              </div>
            </div>
            <div
              className={clsx(styles.card, styles.management)}
            >
              <div className={styles.subTitle}>
                <Translate>
                  Efficient Resource Management
                </Translate>
              </div>
              <div className={styles.line}></div>
              <div className={styles.subDesc}>
                <Translate>
                  Automatic sandbox lifecycle management with configurable resource allocation
                </Translate>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div ref={opensourceRef} className={clsx(styles.opensource, styles.fadeInSection)}>
        <div className={styles.title}>
          <Translate>
            ROCK Open Source Community
          </Translate>
        </div>
        <div className={styles.stats}>
          <div className={styles.statItem}>
            <div className={styles.statNumber}>
              {startCounting && <CountUp end={todayStat?.stars || 287} />}
            </div>
            <div className={styles.statDesc}>
              <Translate>
                Stars
              </Translate>
            </div>
          </div>
          <div className={styles.statItem}>
            <div className={styles.statNumber}>
              {startCounting && <CountUp end={todayStat?.contributors || 12} />}
            </div>
            <div className={styles.statDesc}>
              <Translate>
                Contributors
              </Translate>
            </div>
          </div>
          <div className={styles.statItem}>
            <div className={styles.statNumber}>
              {startCounting && <CountUp end={todayStat?.prs?.total || 212} />}
            </div>
            <div className={styles.statDesc}>
              <Translate>
                PRs
              </Translate>
            </div>
          </div>
        </div>
      </div>
      <div ref={rockRollRef} className={clsx(styles.rock_roll, styles.fadeInSection)}>
        <div className={styles.title2}>
          ROCK&ROLL
        </div>
        <div className={styles.desc2}>
          <Translate>
            The two work together to form a complete closed loop for intelligent agent training.
          </Translate>
        </div>
        <Image src="https://img.alicdn.com/imgextra/i4/O1CN016H9Q8R1oD4hKQqx0q_!!6000000005190-2-tps-2340-837.png" preview={false} style={{ marginTop: 50 }}></Image>
      </div>
      <div ref={communityRef} className={clsx(styles.community, styles.fadeInSection)}>
        <div className={styles.leftTitle}>
          <Translate>
            We welcome contributions from the community!
          </Translate>
        </div>
        <div className={styles.content}>
          <div className={styles.join}>
            <div className={styles.subTitle}>
              <Translate>
                how to get involved
              </Translate>
            </div>
            <p className={styles.subDesc}><span className={styles.bullet}></span><Translate>fork the repository and create a feature branch to make your changes; if applicable, add tests, and then submit a pull request.</Translate></p>
            <p className={styles.subDesc}><span className={styles.bullet}></span><Translate>Please use the GitHub issue tracker to report bugs or suggest features.</Translate></p>
            <p className={styles.subDesc}><span className={styles.bullet}></span><Translate>Follow existing code style and conventions. Please run tests before submitting pull requests.</Translate></p>
          </div>
          <div className={styles.buttons}>
            <Button className={styles.github} target='_blank' href="https://github.com/alibaba/ROCK" variant="outlined" icon={<GithubOutlined />}>
              <Translate>
                GitHub Repository
              </Translate>
            </Button>
            <Button className={styles.btn} onClick={() => setOpen(true)} icon={<WechatOutlined />}><Translate>WeChat</Translate></Button>
            <Button target='_blank' href="https://x.com/FutureLab2025" icon={<XOutlined />} className={styles.btn} variant='outlined'>
              <Translate>
                Follow us on X
              </Translate>
            </Button>
          </div>
        </div>
      </div>
      <div className={styles.footer}>
        <Image src="https://img.alicdn.com/imgextra/i1/O1CN01bxy4yw1GIqkvynPaz_!!6000000000600-0-tps-3840-560.jpg" preview={false}></Image>
      </div>
      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        getContainer={() => document.getElementById('home') || document.body}
      >
        <Image className={styles.whiteImg} src="https://img.alicdn.com/imgextra/i4/O1CN01A2hVeg1OflUzWdvop_!!6000000001733-2-tps-756-850.png" preview={false} />
      </Modal>
    </div >
  </ConfigProvider>

}
