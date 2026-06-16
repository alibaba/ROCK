import React from 'react';
import Layout from '@theme/Layout';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';

import styles from './index.module.css';

const currentRoleLinks = {
  '智能引擎-大模型训练基础架构研发工程师/高级专家-AI Infra':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=1038409&shareCode=tGPVe1DNf9wWpBko553DpI2T4ahZiLrF_NJ7Z_hxUZA%3D',
  '智能引擎-PostTrain框架研发工程师-AI Infra':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=7000016304&shareCode=tGPVe1DNf9wWpBko553DpJi3TFsCjh4syQbtlZp2ujn1yKstq7Sb04s0EC1mY7nf',
  '智能引擎-大模型平台研发工程师-强化学习环境':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100006780014&shareCode=tGPVe1DNf9wWpBko553DpJ3MpTthGYz2ZWV1vShHgx5LHcAG3PQ6rOPZqoRgRIHS',
  '智能引擎-多模态大模型推理系统工程师/专家':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100008580001&shareCode=tGPVe1DNf9wWpBko553DpPENMYSNUyq0L83cQSorzKz4ErFkTMgXh2GL08llVATX',
  '智能引擎-高级引擎研发工程师':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100008580002&shareCode=tGPVe1DNf9wWpBko553DpPENMYSNUyq0L83cQSorzKz%2F_VZfQZv3vM5M5gH1pG4K',
  '智能引擎算法平台-训练系统优化高级工程师/专家':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100008380018&shareCode=tGPVe1DNf9wWpBko553DpKiiMXauM8eAqNmmf_E5AzytA5zLiSVCl54eJZ4QmnGA',
  '智能引擎-机器学习系统工程师':
    'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100008460012&shareCode=tGPVe1DNf9wWpBko553DpNTPm%2Fiu5lvdKCHPRUEo1fUXrexro24t6i77UdIPkYZ2',
  '智能引擎-大模型平台研发工程师-Agent Infra':
    'https://talent-holding.alibaba.com/off-campus/position-detail?lang=zh&positionId=100018640014&spm=a1z8x.8270853.0.0.454b6c5ck9LKl0',
};

const roles = [
  {
    title: '智能引擎-大模型训练基础架构研发工程师/高级专家-AI Infra',
    location: '北京 · 杭州',
  },
  {
    title: '智能引擎-PostTrain框架研发工程师-AI Infra',
    location: '北京 · 杭州',
  },
  {
    title: '智能引擎-大模型平台研发工程师-强化学习环境',
    location: '北京 · 杭州',
  },
  {
    title: '智能引擎-多模态大模型推理系统工程师/专家',
    location: '北京 · 杭州',
  },
  {
    title: '智能引擎-高级搜索平台研发工程师',
    location: '杭州',
    href: 'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100014500002',
  },
  {
    title: '智能引擎-高级引擎研发工程师',
    location: '杭州',
  },
  {
    title: '智能引擎-数据库管控开发工程师',
    location: '杭州',
    href: 'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100010320009',
  },
  {
    title: '智能引擎-项目管理专家-AI项目',
    location: '杭州',
    href: 'https://talent-holding.alibaba.com/off-campus/position-detail?positionId=100011360005',
  },
  {
    title: '智能引擎算法平台-训练系统优化高级工程师/专家',
    location: '北京 · 杭州',
  },
  {
    title: '智能引擎-机器学习系统工程师',
    location: '北京 · 杭州',
  },
  {
    title: '智能引擎-大模型平台研发工程师-Agent Infra',
    location: '北京 · 杭州',
  },
].map((role) => ({
  ...role,
  href: role.href || currentRoleLinks[role.title],
}));

const highlights = [
  {
    zhTitle: '大规模 Agent 训练环境',
    enTitle: 'Large-scale Agent Training Environments',
    zhDesc:
      '面向后训练、评测和数据生产场景，构建大规模智能体训练环境框架 ROCK，支撑强化学习环境的开发、部署和管理。',
    enDesc:
      'Build ROCK, a large-scale agent training environment framework for post-training, evaluation, and data production, covering reinforcement learning environment development, deployment, and management.',
  },
  {
    zhTitle: '大模型基础工程平台',
    enTitle: 'Large Model Engineering Platform',
    zhDesc:
      '构建阿里集团大语言模型和多模态模型的基础工程平台，支撑 HappyHorse、HappyOyster 系列模型的训练、评测、预测和应用开发链路。',
    enDesc:
      'Build foundational engineering platforms for Alibaba Group LLMs and multimodal models, supporting training, evaluation, prediction, and application development for the HappyHorse and HappyOyster model families.',
  },
  {
    zhTitle: '多模态理解与生成推理',
    enTitle: 'Multimodal Understanding and Generation Inference',
    zhDesc:
      '围绕 VL、Omni、DiT、Diffusion Model 和 AR 架构，建设从底层算子到推理服务调度的全栈优化体系，在精度、性能和成本之间取得最佳平衡。',
    enDesc:
      'Optimize the full stack for VL, Omni, DiT, Diffusion Model, and AR architectures, from low-level kernels to inference service scheduling, balancing model quality, performance, and cost.',
  },
  {
    zhTitle: 'HappyHorse 推理工程落地',
    enTitle: 'HappyHorse Inference Engineering',
    zhDesc:
      '负责行业顶尖多模态理解和生成模型在 AI 创新应用场景的推理落地，覆盖推理架构设计、全链路服务编排和性能极致优化。',
    enDesc:
      'Deliver production inference for leading multimodal understanding and generation models in AI applications, spanning inference architecture, end-to-end service orchestration, and performance optimization.',
  },
];

const papers = [
  {
    title: 'KDD 2026 UniRank: End-to-End Domain-Specific Reranking of Hybrid text-image Candidates',
    href: 'https://arxiv.org/abs/2603.29897',
  },
  {
    title: 'ICML 2026 Factored Causal Representation Learning for Robust Reward Modeling in RLHF',
    href: 'https://arxiv.org/abs/2601.21350',
  },
];

const projects = [
  {
    name: 'Megatron-LLaMA',
    tags: ['LLM', 'PyTorch'],
    href: 'https://github.com/alibaba/Megatron-LLaMA',
    zhDesc: '基于 Megatron 的大语言模型开源训练框架，支持高效分布式 LLM 训练。',
    enDesc: 'An open-source LLM training framework based on Megatron, supporting efficient distributed training.',
  },
  {
    name: 'X-DeepLearning (XDL)',
    tags: ['Sparse', 'RecSys'],
    href: 'https://github.com/alibaba/x-deeplearning',
    zhDesc: '阿里巴巴开源的稀疏模型训练框架，支持大规模推荐和广告场景。',
    enDesc: "Alibaba's open-source sparse model training framework for large-scale recommendation and advertising.",
  },
  {
    name: 'ROLL',
    tags: ['RL', 'Distributed'],
    href: 'https://github.com/alibaba/ROLL',
    zhDesc: '强化学习开源训练框架，支持大规模 RL Post-Training 的高效分布式执行。',
    enDesc: 'An open-source RL training framework for efficient distributed RL post-training at scale.',
  },
  {
    name: 'ROCK',
    tags: ['RL Env', 'Agent'],
    href: 'https://github.com/alibaba/ROCK',
    zhDesc: '开源的强化学习环境开发框架，旨在简化强化学习环境的开发、部署和管理流程。',
    enDesc:
      'An open-source reinforcement learning environment development framework that simplifies environment development, deployment, and management.',
  },
  {
    name: 'Euler',
    tags: ['GNN', 'Graph'],
    href: 'https://github.com/alibaba/euler',
    zhDesc: '阿里巴巴开源的分布式图学习引擎，支持大规模图神经网络训练。',
    enDesc: "Alibaba's open-source distributed graph learning engine for large-scale GNN training.",
  },
  {
    name: 'RecIS',
    tags: ['RecSys', 'Training'],
    href: 'https://github.com/alibaba/RecIS',
    zhDesc: '预估大模型训练框架，面向推荐和广告场景的工业级大模型训练系统。',
    enDesc: 'A large model training framework for recommendation and advertising at industrial scale.',
  },
];

const copy = {
  zh: {
    pageTitle: '加入 ROCK 与 AI Infra 团队',
    pageDescription: '阿里控股智能引擎 AI Infra 团队招聘信息。',
    eyebrow: 'Alibaba Intelligent Engine · AI Infra',
    heroTitle: '加入 ROCK 与智能引擎 AI Infra 团队',
    heroDesc: [
      '智能引擎部门隶属于阿里巴巴控股集团平台技术，主要负责阿里集团内部搜推广工程系统和 AI 相关工程系统的开发和能力建设，同时 AIGC 的蓬勃发展为智能引擎注入了新的活力：如何解决大模型的训推等性能和成本问题，如何优化大模型研发范式、应对大模型在线服务带来的新挑战，都是我们在 AI 时代下的新命题。',
      '我们致力于为集团用户提供从数据开始，到训练、评测、预测、应用开发、解决方案的完整工程体系。团队和 Happy 家族模型算法团队密切协同，共同研发多模态生成大模型，承担阿里集团多模态生成模型研发的重任。',
      '我们拥有浓厚的技术氛围，本着开源共享的精神贡献了 ROLL、Megatron-LLaMA、XDL、Euler 等众多优秀的开源产品，坚持以开放的姿态与行业共享成果、共赢共进。',
    ],
    introTitle: '我们是谁',
    introDesc:
      '搜索&管控平台负责构建阿里集团大语言模型和多模态模型的基础工程平台，团队致力于为大语言模型提供大规模 Agent 训练环境，也为 HappyHorse、HappyOyster 系列模型提供极致的推理服务能力。',
    highlightsTitle: '方向亮点',
    rolesTitle: '开放岗位',
    rolesDesc: '点击岗位名称或投递按钮进入阿里人才页面。',
    apply: '投递',
    locationLabel: '地点',
    papersTitle: '关键成果精选',
    papersDesc:
      '我们是阿里 HappyHorse 推理工程团队，负责行业顶尖的多模态理解和生成模型在 AI 创新应用场景的推理落地，涵盖从推理架构设计、全链路服务编排到性能极致优化的全栈技术挑战。',
    openSourceTitle: '开源训练框架与系统',
    openSourceEyebrow: 'Open Source',
    joinTitle: '如何加入',
    joinSteps: [
      '选择上方岗位，进入阿里控股招聘页面登录后投递。',
      '也可以一键投递到邮箱 lxm02049624@alibaba-inc.com，并添加 HR 微信咨询：19952378952。',
      '联系发布岗位信息的阿里同学帮忙内推。',
    ],
  },
  en: {
    pageTitle: 'Join ROCK and the AI Infra Team',
    pageDescription: 'Open roles for Alibaba Intelligent Engine AI Infra team.',
    eyebrow: 'Alibaba Intelligent Engine · AI Infra',
    heroTitle: 'Join ROCK and the Intelligent Engine AI Infra Team',
    heroDesc: [
      "The Intelligent Engine department belongs to Alibaba Holding Group's Platform Technology organization. It builds internal search, recommendation, advertising, and AI engineering systems. The rapid growth of AIGC brings new challenges: improving large-model training and inference performance and cost, reshaping model development workflows, and addressing new demands from online model services.",
      'We provide a complete engineering system for group users, spanning data, training, evaluation, prediction, application development, and solutions. Working closely with the Happy family model teams, we co-develop multimodal generative large models and take on core responsibilities for Alibaba Group multimodal generation model development.',
      'We value a strong technical culture and open sharing. The team has contributed open-source projects including ROLL, Megatron-LLaMA, XDL, and Euler, and continues to share outcomes with the broader community.',
    ],
    introTitle: 'Who We Are',
    introDesc:
      'The Search & Governance Platform builds foundational engineering platforms for Alibaba Group LLMs and multimodal models. The team provides large-scale agent training environments for LLMs and high-performance inference services for the HappyHorse and HappyOyster model families.',
    highlightsTitle: 'Focus Areas',
    rolesTitle: 'Open Roles',
    rolesDesc:
      'Select a role or apply button to open the Alibaba Talent page.',
    apply: 'Apply',
    locationLabel: 'Location',
    papersTitle: 'Selected Results',
    papersDesc:
      'As the Alibaba HappyHorse inference engineering team, we bring leading multimodal understanding and generation models into AI applications, covering inference architecture design, end-to-end service orchestration, and deep performance optimization.',
    openSourceTitle: 'Open Source Frameworks and Systems',
    openSourceEyebrow: 'Open Source',
    joinTitle: 'How to Join',
    joinSteps: [
      'Choose a role above and submit your application through Alibaba Talent.',
      'You can also send your resume to lxm02049624@alibaba-inc.com and contact HR on WeChat: 19952378952.',
      'Ask an Alibaba colleague who shared the role to refer you internally.',
    ],
  },
};

function ExternalRoleLink({ role, children, className }) {
  return (
    <a className={className} href={role.href} target="_blank" rel="noreferrer">
      {children}
    </a>
  );
}

export default function Careers() {
  const { i18n } = useDocusaurusContext();
  const isChinese = i18n.currentLocale !== 'en';
  const text = isChinese ? copy.zh : copy.en;
  const scrollToRoles = () => {
    document.getElementById('open-roles')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <Layout title={text.pageTitle} description={text.pageDescription}>
      <main className={styles.page}>
        <section className={styles.hero}>
          <div className={`container ${styles.heroContainer}`}>
            <div className={styles.heroInner}>
              <div className={styles.eyebrow}>{text.eyebrow}</div>
              <h1>{text.heroTitle}</h1>
              <div className={styles.heroDesc}>
                {text.heroDesc.map((paragraph) => (
                  <p key={paragraph}>{paragraph}</p>
                ))}
              </div>
              <div className={styles.heroActions}>
                <button className="button button--primary" type="button" onClick={scrollToRoles}>
                  {text.rolesTitle}
                </button>
              </div>
            </div>
          </div>
        </section>

        <div className="container">
          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <p>{text.introTitle}</p>
              <h2>{text.highlightsTitle}</h2>
            </div>
            <p className={styles.lead}>{text.introDesc}</p>
            <div className={styles.highlightGrid}>
              {highlights.map((item) => (
                <article className={styles.highlightCard} key={item.enTitle}>
                  <h3>{isChinese ? item.zhTitle : item.enTitle}</h3>
                  <p>{isChinese ? item.zhDesc : item.enDesc}</p>
                </article>
              ))}
            </div>
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <p>Selected Results</p>
              <h2>{text.papersTitle}</h2>
            </div>
            <p className={styles.lead}>{text.papersDesc}</p>
            <div className={styles.paperGrid}>
              {papers.map((paper) => (
                <article className={styles.paperCard} key={paper.href}>
                  <h3>{paper.title}</h3>
                  <a href={paper.href} target="_blank" rel="noreferrer">
                    {paper.href}
                  </a>
                </article>
              ))}
            </div>
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHeader}>
              <p>{text.openSourceEyebrow}</p>
              <h2>{text.openSourceTitle}</h2>
            </div>
            <div className={styles.projectGrid}>
              {projects.map((project) => (
                <article className={styles.projectCard} key={project.name}>
                  <h3>{project.name}</h3>
                  <p>{isChinese ? project.zhDesc : project.enDesc}</p>
                  <div className={styles.projectFooter}>
                    <div className={styles.projectTags}>
                      {project.tags.map((tag) => (
                        <span className={styles.projectTag} key={tag}>
                          {tag}
                        </span>
                      ))}
                    </div>
                    <a href={project.href} target="_blank" rel="noreferrer">
                      GitHub
                    </a>
                  </div>
                </article>
              ))}
            </div>
          </section>

          <section className={`${styles.section} ${styles.rolesSection}`} id="open-roles">
            <div className={styles.sectionHeader}>
              <p>Open Roles</p>
              <h2>{text.rolesTitle}</h2>
            </div>
            <p className={styles.lead}>{text.rolesDesc}</p>
            <div className={styles.roleList}>
              {roles.map((role) => (
                <article className={styles.roleRow} key={role.title}>
                  <div className={styles.roleMain}>
                    <ExternalRoleLink role={role} className={styles.roleTitle}>
                      {role.title}
                    </ExternalRoleLink>
                    <div className={styles.roleMeta}>
                      <span>
                        {text.locationLabel}: {role.location}
                      </span>
                    </div>
                  </div>
                  <ExternalRoleLink role={role} className={styles.roleAction}>
                    {text.apply}
                  </ExternalRoleLink>
                </article>
              ))}
            </div>
          </section>

          <section className={styles.joinSection}>
            <div className={styles.sectionHeader}>
              <p>Application</p>
              <h2>{text.joinTitle}</h2>
            </div>
            <ol className={styles.joinList}>
              {text.joinSteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </section>
        </div>
      </main>
    </Layout>
  );
}
