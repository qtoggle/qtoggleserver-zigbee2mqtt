from setuptools import setup, find_namespace_packages


setup(
    name='qtoggleserver-zigbee2mqtt',
    version='unknown-version',
    description='qToggleServer integration with Zigbee2MQTT',
    author='Calin Crisan',
    author_email='ccrisan@gmail.com',
    license='Apache 2.0',

    packages=find_namespace_packages(),

    install_requires=[
        'aiomqtt>=2.0',
    ]
)
