import React from 'react';
import Navbar from '@theme-original/Navbar';
import { useLocation } from '@docusaurus/router';

export default function NavbarWrapper(props) {
  const location = useLocation();
  const isHomepage = location.pathname === '/' || location.pathname === '/ROCK/' || location.pathname === '/ROCK/zh-Hans/';

  if (isHomepage) {
    return null;
  }

  return (
    <>
      <Navbar {...props} />
    </>
  );
}
