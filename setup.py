from distutils.core import setup

setup(name='electrumsv-database',
      version='1.6',
      description='Database support for use of SQLite (possibly other databases later).',
      author='Roger Taylor',
      author_email='roger.taylor.email@gmail.com',
      url='https://github.com/electrumsv/electrumsv-database',
      long_description=open('README.md', 'r').read(),
      long_description_content_type='text/markdown',
      license='MIT license',
      packages=['electrumsv_database'],
      package_data={ "electrumsv_database": ["py.typed"] },
      install_requires=[
          'pysqlite3-binary; platform_system=="Linux"'
      ])
