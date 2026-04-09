import React, { useEffect, useState } from 'react';
import Layout from '@theme/Layout';
import Statistics from '../../components/StatsClient';
import StatChart from '../../components/StatChartClient';

export default () => {
  const [todayStat, setTodayStat] = useState({});
  const [allStat, setAllStat] = useState({});
  const today = new Date().toISOString().slice(0, 10);
  useEffect(() => {
    fetch('/ROCK/stats.json').then(res => res.json()).then(data => {
      setTodayStat(data[today]);
      setAllStat(data);
    })
  }, []);

  return <Layout>
    <main>
      <Statistics todayStat={todayStat} />
      <StatChart allStat={allStat} />
      <div style={{ width: '80%', margin: '0 auto', marginTop: '20px' }}>
        文档页面详细统计可以打开<a target='_blank' href="https://analytics.google.com/analytics/web/#/a375760323p513982501/reports/reportinghub?params=_u..nav%3Dmaui%26_u.dateOption%3Dyesterday%26_u.comparisonOption%3Ddisabled">Google analytics</a>查看
      </div>
    </main>
  </Layout>;
}