import React, { useState, useEffect } from 'react';
import dayjs from 'dayjs';
import useIsBrowser from '@docusaurus/useIsBrowser';
import { Card, theme, ConfigProvider, DatePicker } from 'antd';
import EchartsViewClient from '../EchartsClient';
import locale from 'antd/locale/zh_CN';

import 'dayjs/locale/zh-cn';
import useIsDark from '../StatsClient/useIsDark';

dayjs.locale('zh-cn');

const { RangePicker } = DatePicker;

export default ({ allStat }) => {
  const isBrowser = useIsBrowser();
  const isDark = useIsDark();
  const today = dayjs().format('YYYY-MM-DD');
  const defaultDates = [dayjs().subtract(7, 'day'), dayjs()];
  const [dates, setDates] = useState(defaultDates);
  const [chartData, setChartData] = useState([]);

  useEffect(() => {
    if (dates && dates.length === 2 && dates[0] && dates[1]) {
      const startDate = dayjs(dates[0]);
      const endDate = dayjs(dates[1]);

      const dateArray = [];
      let currentDate = startDate.clone();
      while (currentDate.isBefore(endDate) || currentDate.isSame(endDate)) {
        dateArray.push(currentDate.format('YYYY-MM-DD'));
        currentDate = currentDate.add(1, 'day');
      }

      const filteredData = dateArray.map(date => ({
        date,
        ...(allStat[date] || {})
      })).filter(item => item.date);

      setChartData(filteredData);
    }
  }, [dates, allStat]);

  if (!isBrowser) return null;

  return <ConfigProvider locale={locale} theme={{ algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
    <div style={{ width: '80%', margin: '0 auto', marginTop: 20 }}>
      <RangePicker defaultValue={defaultDates} onChange={dates => setDates(dates)} presets={[{ label: '最近7天', value: [dayjs().subtract(7, 'day'), dayjs()] }, { label: '最近30天', value: [dayjs().subtract(30, 'day'), dayjs()] }]} />
      <Card title={`${dates.map(item => item.format('YYYY-MM-DD')).join('——')}数据统计图`}>
        <EchartsViewClient
          option={{
            tooltip: {
              trigger: 'axis'
            },
            legend: {
              data: ['Fork数', 'Star数', 'Contributors', 'issues总数', 'issues open数', 'issue 解决率', 'PR总数', 'PR open数'],
              bottom: 20,
            },
            xAxis: {
              type: 'category',
              boundaryGap: false,
              data: chartData.map(item => item.date),
            },
            yAxis: {
              type: 'value'
            },
            series: [
              {
                name: 'Fork数',
                type: 'line',
                data: chartData.map(item => item.forks || 0)
              },
              {
                name: 'Star数',
                type: 'line',
                data: chartData.map(item => item.stars || 0)
              },
              {
                name: 'Contributors',
                type: 'line',
                data: chartData.map(item => item.contributors || 0)
              },
              {
                name: 'issues总数',
                type: 'line',
                data: chartData.map(item => item.issues?.total || 0)
              },
              {
                name: 'issues open数',
                type: 'line',
                data: chartData.map(item => item.issues?.open || 0)
              },
              {
                name: 'issue 解决率',
                type: 'line',
                data: chartData.map(item => item.issues?.fixRate || 0)
              },
              {
                name: 'PR总数',
                type: 'line',
                data: chartData.map(item => item.prs?.total || 0)
              },
              {
                name: 'PR open数',
                type: 'line',
                data: chartData.map(item => item.prs?.open || 0)
              },
            ],
          }}
          isDark={isDark}
          style={{ widht: '100%', height: 400 }}
        />
      </Card>
    </div>
  </ConfigProvider>
}
